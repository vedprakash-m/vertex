"""ADF-W3.7 remainder: local reply import.

Same credential-free, no-API pattern ADR-008/REV already established for
local `.eml` import (Section 8.10.8's reply-ingestion step): the
stakeholder replies to a solicitation draft (in their own mail client),
then the operator saves that reply as a local `.eml` file and drops it
into `programs/<id>/nudge/replies/`. This module enumerates and parses
those files -- it never touches a live mailbox.

Deliberately simpler than `src/m365/rev/eml_hydrator.py`: REV's hydrator is
built for high-volume mailbox import with its own candidate/correlation
pipeline; a handful of solicitation replies per week does not need that
machinery. This module is a small, standalone stdlib `email` parser.
"""

from __future__ import annotations

from email import message_from_bytes
from email.message import Message
from email.utils import parseaddr
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import yaml

from src.core.context_gap_reply import ParsedReply
from src.core.context_gap_solicitation import validate_solicitation_draft
from src.core.edition_resolver import PROGRAMS_ROOT, get_nudge_paths
from src.core.context_gap_store import ContextGapRecord, _gap_path, rank_context_gaps
from src.core.jsonl_utils import parse_jsonl_line
from src.core.manual_registry_diff import read_registry_baseline
from src.core.ncfl_models import ContextUpdateProposal, EXTRACTION_METHOD_CONFIDENCE, NCFL_EXTRACTOR_VERSION
from src.core.ncfl_proposal_store import insert_proposal, load_proposals
from src.core.rev.normalizer import normalize_html_to_text
from src.core.workspace_paths import is_bare_identifier

_REFERENCE_MARKER_PREFIX = "Reference: "


class ContextGapReplyImportError(ValueError):
    """A local reply cannot be associated safely with the selected gap."""


def _require_program_path(path: Path, program: Path) -> None:
    if not path.resolve().is_relative_to(program):
        raise ContextGapReplyImportError("Context input or queue leaves the selected program.")


def _read_local_records(path: Path, program: Path) -> list[dict]:
    _require_program_path(path, program)
    records = [parse_jsonl_line(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ContextGapReplyImportError("Invalid context record.")
    return records


def import_context_gap_reply(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    source: Path,
    gap_fingerprint: str,
    solicitation_id: str,
    programs_root: Path,
    dry_run: bool = False,
) -> ContextUpdateProposal:
    """Stage an unverified local reply only; never decide or apply it."""
    if not program_id or program_id in {".", ".."} or any(char in program_id for char in "/\\:"):
        raise ContextGapReplyImportError("Invalid program identifier.")
    program = (programs_root / program_id).resolve()
    if not program.is_relative_to(programs_root.resolve()):
        raise ContextGapReplyImportError("Program path leaves the selected root.")
    source = source.resolve()
    if source.is_relative_to(programs_root.resolve()) and not source.is_relative_to(program):
        raise ContextGapReplyImportError("Reply source belongs to another program.")
    if source.suffix.lower() != ".eml" or issue_number < 1:
        raise ContextGapReplyImportError("Select a local .eml file and a positive issue number.")
    try:
        gaps = [
            ContextGapRecord.from_json(record)
            for record in _read_local_records(_gap_path(program_id, programs_root=programs_root), program)
        ]
        if any(gap.program != program_id for gap in gaps):
            raise ContextGapReplyImportError("Gap observations contain a foreign program.")
        selected = [
            gap for gap in rank_context_gaps(gaps)
            if f"{gap.program}:{gap.feature}:{gap.lane or ''}:{gap.field}" == gap_fingerprint
        ]
        if len(selected) != 1:
            raise ContextGapReplyImportError("Select one existing program gap.")
        gap = selected[0]
        field = gap.field.removeprefix("deep_context.")
        if not gap.lane or field not in {"why", "what", "how"}:
            raise ContextGapReplyImportError("Only workstream deep_context.why/what/how replies are supported.")
        log = program / "_feedback" / "context_gap_solicitations.jsonl"
        records = _read_local_records(log, program)
        associated = [record for record in records if record.get("solicitation_id") == solicitation_id]
        if len(associated) != 1 or associated[0].get("gap_fingerprint") != gap_fingerprint:
            raise ContextGapReplyImportError("Solicitation does not uniquely match the selected program gap.")
        record = associated[0]
        target_field = f"deep_context.{field}"
        bound = solicitation_id.startswith("solicitation-v1-") or any(
            key in record for key in (
                "packet_version", "program_id", "target_store", "target_key", "target_field",
                "current_value", "current_value_hash", "eml_sha256",
            )
        )
        if bound:
            if not is_bare_identifier(solicitation_id):
                raise ContextGapReplyImportError("Solicitation identifier must be a bare packet identifier.")
            draft = get_nudge_paths(program_id, programs_root=programs_root).drafts_dir / f"{solicitation_id}.eml"
            if not validate_solicitation_draft(program_id, draft, programs_root=programs_root):
                raise ContextGapReplyImportError("Missing solicitation packet binding.")
            if (
                not (
                    gap.feature == "workstream_registry"
                    or gap.feature == "doctor --context" and gap.field in {"deep_context.why", "deep_context.what"}
                )
                or record.get("program_id") != program_id
                or record.get("target_store") != "workstream_registry"
                or record.get("target_key") != gap.lane or record.get("target_field") != target_field
            ):
                raise ContextGapReplyImportError("Solicitation packet target does not match the selected gap.")
        raw = source.read_bytes()
        message = message_from_bytes(raw)
        body = _extract_body_text(message)
        markers = {
            line.strip().lstrip(">").strip()[len(_REFERENCE_MARKER_PREFIX):].strip()
            for line in body.splitlines()
            if line.strip().lstrip(">").strip().startswith(_REFERENCE_MARKER_PREFIX)
        }
        if markers != {solicitation_id}:
            raise ContextGapReplyImportError("Missing, ambiguous or unmatched unverified Reference.")
        answer = "\n".join(
            line for line in _isolate_new_content(body).splitlines()
            if not line.strip().lstrip(">").strip().startswith(_REFERENCE_MARKER_PREFIX)
        ).strip()
        if not answer:
            raise ContextGapReplyImportError("Reply contains no proposed answer.")
        identity = json.dumps((program_id, edition_id, solicitation_id, gap_fingerprint, sha256(raw).hexdigest()))
        proposal_id = "reply-" + sha256(identity.encode("utf-8")).hexdigest()[:24]
        queue = program / "context_proposals"
        _require_program_path(queue, program)
        for path in queue.glob("issue_*.proposals.json"):
            _require_program_path(path, program)
        _require_program_path(queue / "_conflict_index.json", program)
        _require_program_path(queue / "_queue.lock", program)
        all_proposals = load_proposals(program_id, programs_root=programs_root)
        if any(proposal.program_id != program_id for proposal in all_proposals):
            raise ContextGapReplyImportError("Proposal queue contains a foreign program.")
        existing = next((proposal for proposal in all_proposals if proposal.proposal_id == proposal_id), None)
        if existing is not None:
            return existing
        if bound:
            current, baseline = record["current_value"], record["current_value_hash"]
            binding_rationale = "validated local packet-time baseline binding, not origin verification or approval."
        else:
            current, baseline = read_registry_baseline(program_id, gap.lane, target_field, programs_root=programs_root)
            binding_rationale = "intake-time baseline only, not packet-time revision binding or approval."
        rationale = (
            f"Observed From {str(message.get('From', ''))!r} and Reference {solicitation_id!r} are unverified; "
            f"local EML SHA-256 {sha256(raw).hexdigest()}; {binding_rationale}"
        )
        proposal = ContextUpdateProposal(
            proposal_id=proposal_id, program_id=program_id, edition_id=edition_id, issue_number=issue_number,
            source_type="context_gap_reply", extracted_at=datetime.now(timezone.utc),
            extractor_version=NCFL_EXTRACTOR_VERSION, source_artifact=str(source), source_field="body",
            extraction_method="field_update_email", target_store="workstream_registry",
            target_key=gap.lane, target_field=target_field, source_value=answer,
            current_value=current, current_value_hash=baseline,
            confidence=EXTRACTION_METHOD_CONFIDENCE["field_update_email"], batch_eligible=False,
            extraction_method_rationale=rationale,
            conflict_key=f"workstream_registry:{gap.lane}:{target_field}",
        )
        if not dry_run:
            return insert_proposal(program_id, proposal, programs_root=programs_root)
        return proposal
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        if isinstance(error, ContextGapReplyImportError):
            raise
        raise ContextGapReplyImportError(f"Reply intake unavailable ({type(error).__name__}).") from error


def replies_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "nudge" / "replies"


def list_pending_replies(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ...]:
    directory = replies_dir(program_id, programs_root=programs_root)
    if not directory.exists():
        return ()
    return tuple(sorted(directory.glob("*.eml")))


def parse_reply_eml(path: Path) -> ParsedReply:
    raw = path.read_bytes()
    message = message_from_bytes(raw)
    sender_email = _extract_sender_email(message)
    subject = str(message.get("Subject", "")).strip()
    body_text = _extract_body_text(message)
    reference_marker = _extract_reference_marker(body_text)
    return ParsedReply(
        sender_email=sender_email,
        subject=subject,
        body_text=_isolate_new_content(body_text),
        reference_marker=reference_marker,
    )


def _extract_sender_email(message: Message) -> str | None:
    raw_from = message.get("From")
    if not raw_from:
        return None
    _, email_address = parseaddr(str(raw_from))
    return email_address or None


def _extract_body_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.is_multipart():
                return _decode(part)
        for part in message.walk():
            if part.get_content_type() == "text/html" and not part.is_multipart():
                return _strip_html_tags(_decode(part))
        return ""
    text = _decode(message)
    return _strip_html_tags(text) if message.get_content_type() == "text/html" else text


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload())
    if not isinstance(payload, bytes):
        return str(payload)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _strip_html_tags(html: str) -> str:
    text = normalize_html_to_text(html)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _extract_reference_marker(body_text: str) -> str | None:
    for line in body_text.splitlines():
        stripped = line.strip().lstrip(">").strip()
        if stripped.startswith(_REFERENCE_MARKER_PREFIX):
            return stripped[len(_REFERENCE_MARKER_PREFIX) :].strip()
    return None


# Common top-posted-reply separators most mail clients insert before quoted
# original content. Best-effort: if none match, the full body is treated as
# the "new" content -- a human still reviews it before anything is applied,
# so a missed separator degrades to "extra quoted text visible," not a
# silent wrong answer.
_QUOTE_SEPARATORS = (
    "-----Original Message-----",
    "________________________________",
)


def _isolate_new_content(body_text: str) -> str:
    earliest_cut: int | None = None
    for separator in _QUOTE_SEPARATORS:
        index = body_text.find(separator)
        if index != -1 and (earliest_cut is None or index < earliest_cut):
            earliest_cut = index
    for line in body_text.splitlines():
        if line.strip().startswith("On ") and line.strip().endswith("wrote:"):
            index = body_text.find(line)
            if index != -1 and (earliest_cut is None or index < earliest_cut):
                earliest_cut = index
            break
    if earliest_cut is None:
        return body_text.strip()
    return body_text[:earliest_cut].strip()


__all__ = [
    "ContextGapReplyImportError", "import_context_gap_reply",
    "list_pending_replies", "parse_reply_eml", "replies_dir",
]
