"""Pending context-gap drafts, packet-time binding and existing nudge cooldown.

Explicit nudge approval is the sole approval lifecycle. Drafts never authorize
a send, validate recipient authority, renew review or resolve a context gap.
Local reply intake separately stages unverified proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from email import message_from_bytes
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

import portalocker
import yaml

from src.core.context_gap_store import ContextGapRecord, RankedGap, _gap_path, rank_context_gaps
from src.core.edition_resolver import PROGRAMS_ROOT, get_nudge_paths
from src.core.eml_writer import build_eml_bytes, write_eml_atomic
from src.core.exceptions import StateError
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line
from src.core.manual_registry_diff import read_registry_baseline
from src.core.nudge_models import NUDGE_STATE_LOCK_TIMEOUT_SECONDS, ResolvedRecipient
from src.core.reality_store import get_program_reality_db_path
from src.core.workspace_paths import is_bare_identifier

_DEFAULT_COOLDOWN_DAYS = 14  # matches INV-14's nudge cooldown convention
_PACKET_PREFIX = "solicitation-v1-"


class ContextGapSolicitationError(ValueError):
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
    target_key: str | None = None
    target_field: str | None = None
    current_value: str | None = None
    current_value_hash: str | None = None


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
    for record in _read_records(path):
        if not all(isinstance(record.get(key), str) and record[key] for key in ("gap_fingerprint", "solicitation_id", "drafted_at")):
            raise ContextGapSolicitationError("Invalid solicitation cooldown record.")
        try:
            drafted_at = datetime.fromisoformat(str(record["drafted_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError) as error:
            raise ContextGapSolicitationError("Invalid solicitation cooldown timestamp.") from error
        if drafted_at.tzinfo is None:
            raise ContextGapSolicitationError("Solicitation cooldown timestamp must include a timezone.")
        if record["gap_fingerprint"] == fingerprint and drafted_at.timestamp() >= cutoff:
            return True
    return False


def record_solicitation_drafted(
    solicitation: ContextGapSolicitation, *, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None,
    eml_sha256: str | None = None,
) -> None:
    """Appends a cooldown-tracking record. Call once, immediately after
    ``write_solicitation_draft`` succeeds -- this is what
    ``is_in_cooldown`` reads."""
    path = _cooldown_log_path(solicitation.program_id, programs_root=programs_root)
    reference = now or datetime.now(timezone.utc)
    payload: dict[str, str | int | None] = {
        "gap_fingerprint": solicitation.gap_fingerprint,
        "solicitation_id": solicitation.id,
        "drafted_at": reference.isoformat().replace("+00:00", "Z"),
    }
    if solicitation.current_value_hash is not None:
        payload.update(
            packet_version=1, program_id=solicitation.program_id, target_store="workstream_registry",
            target_key=solicitation.target_key, target_field=solicitation.target_field,
            current_value=solicitation.current_value, current_value_hash=solicitation.current_value_hash,
            eml_sha256=eml_sha256,
        )
    separator = ""
    if path.exists():
        with path.open("rb") as handle:
            if handle.seek(0, 2):
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    separator = "\n"
    append_jsonl_line(path, separator + json.dumps(payload, ensure_ascii=False) + "\n")


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
    """Write a pending draft; only the existing nudge approval index records approval."""
    if not is_bare_identifier(solicitation.id):
        raise ContextGapSolicitationError("Solicitation identifier must be a bare packet identifier.")
    _validate_paths(solicitation.program_id, programs_root)
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
    if solicitation.current_value_hash is not None:
        text_body += (
            f"\nProgram: {solicitation.program_id}\n"
            f"Target: workstream_registry:{solicitation.target_key}:{solicitation.target_field}\n"
            f"Packet-time value: {solicitation.current_value}\n"
            f"Packet-time baseline: {solicitation.current_value_hash}\n"
        )
    # ADF-W3.7 remainder: a stable reference line, preserved verbatim in most
    # mail clients' quoted-reply content, is how context_gap_reply_import.py
    # correlates an inbound reply back to this exact solicitation -- reply
    # threading headers (Message-ID/In-Reply-To) are not reliable once a
    # reply is manually exported back to a local .eml by a different client.
    text_body += f"\nReference: {solicitation.id}\n"
    html_body = "<br>".join(escape(line) if line else "<br>" for line in text_body.splitlines())

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
    if solicitation.current_value_hash is not None:
        binding_parts: tuple[str | None, ...] = (
            solicitation.program_id, solicitation.target_key, solicitation.target_field,
            solicitation.current_value, solicitation.current_value_hash,
        )
        if solicitation.gap_fingerprint.startswith(f"{solicitation.program_id}:doctor --context:"):
            binding_parts += (solicitation.gap_fingerprint,)
        binding = json.dumps(binding_parts, ensure_ascii=False)
        eml_bytes = (
            f"X-Vertex-Solicitation: {solicitation.id}\r\n"
            f"X-Vertex-Packet-Binding: {sha256(binding.encode('utf-8')).hexdigest()}\r\n"
        ).encode("ascii") + eml_bytes
    drafts_dir = get_nudge_paths(solicitation.program_id, programs_root=programs_root).drafts_dir
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{solicitation.id}.eml"
    if draft_path.exists():
        raise ContextGapSolicitationError("A solicitation packet cannot overwrite an existing draft.")
    return write_eml_atomic(draft_path, eml_bytes=eml_bytes)


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = [parse_jsonl_line(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ContextGapSolicitationError("Invalid context record.")
    return records


def _validate_paths(program_id: str, programs_root: Path) -> Path:
    paths = get_nudge_paths(program_id, programs_root=programs_root)
    if not programs_root.is_absolute():
        raise ContextGapSolicitationError("Programs root must be absolute.")
    program = (programs_root / program_id).resolve()
    if not program.is_relative_to(programs_root.resolve()):
        raise ContextGapSolicitationError("Program leaves the selected root.")
    candidates = [
        *(getattr(paths, field.name) for field in fields(paths)),
        paths.nudge_root / "draft_approvals.json",
        paths.nudge_root / "rotated",
        paths.audit_path.with_suffix(".sha256"),
        paths.state_path.with_suffix(".json.lock"),
        _gap_path(program_id, programs_root=programs_root),
        program / "_feedback" / "context_gap_solicitations.jsonl",
        program / "_feedback" / "context_gap_solicitations.sha256",
    ]
    if any(not path.resolve().is_relative_to(program) for path in candidates):
        raise ContextGapSolicitationError("Solicitation storage leaves the selected program.")
    return program


def produce_context_gap_solicitation(
    *, program_id: str, gap_fingerprint: str, recipient_email: str,
    programs_root: Path, dry_run: bool = False,
) -> ContextGapSolicitation:
    """Prepare one revision-bound pending packet without approving or sending."""
    try:
        program = _validate_paths(program_id, programs_root)
        if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", recipient_email):
            raise ContextGapSolicitationError("Select one plain recipient email address; its authority is unverified.")
        records = [
            ContextGapRecord.from_json(row)
            for row in _read_records(_gap_path(program_id, programs_root=programs_root))
        ]
        if any(record.program != program_id for record in records):
            raise ContextGapSolicitationError("Gap observations contain a foreign program.")
        selected = [
            gap for gap in rank_context_gaps(records, programs_root=programs_root)
            if _gap_fingerprint(gap) == gap_fingerprint
        ]
        if len(selected) != 1:
            raise ContextGapSolicitationError("Select one active existing program gap.")
        gap = selected[0]
        canonical = gap.feature == "workstream_registry" and gap.field in {
            "deep_context.why", "deep_context.what", "deep_context.how",
        }
        structural = gap.feature == "doctor --context" and gap.field in {"deep_context.why", "deep_context.what"}
        if not gap.lane or not (canonical or structural):
            raise ContextGapSolicitationError("Only workstream deep_context.why/what/how solicitations are supported.")
        if is_in_cooldown(gap, programs_root=programs_root):
            raise ContextGapSolicitationError("The selected gap is in solicitation cooldown.")
        current, baseline = read_registry_baseline(program_id, gap.lane, gap.field, programs_root=programs_root)
        solicitation = replace(
            generate_deterministic_solicitation(
                gap, recipient=ResolvedRecipient(alias=recipient_email, email=recipient_email, display_name=""),
            ),
            id=_PACKET_PREFIX + uuid4().hex, target_key=gap.lane, target_field=gap.field,
            current_value=current, current_value_hash=baseline,
        )
        if not dry_run:
            paths = get_nudge_paths(program_id, programs_root=programs_root)
            paths.nudge_root.mkdir(parents=True, exist_ok=True)
            with portalocker.Lock(str(paths.run_lock_path), mode="a+", timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS):
                if not any(
                    _gap_fingerprint(active) == gap_fingerprint
                    for active in rank_context_gaps(records, programs_root=programs_root)
                ):
                    raise ContextGapSolicitationError("Select one active existing program gap.")
                if is_in_cooldown(gap, programs_root=programs_root):
                    raise ContextGapSolicitationError("The selected gap is in solicitation cooldown.")
                if read_registry_baseline(program_id, gap.lane, gap.field, programs_root=programs_root) != (current, baseline):
                    raise ContextGapSolicitationError("The selected target changed while preparing the packet.")
                draft = write_solicitation_draft(solicitation, from_email=None, programs_root=programs_root)
                record_solicitation_drafted(
                    solicitation, programs_root=programs_root, eml_sha256=sha256(draft.read_bytes()).hexdigest(),
                )
        return solicitation
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, StateError, portalocker.exceptions.LockException) as error:
        if isinstance(error, ContextGapSolicitationError):
            raise
        raise ContextGapSolicitationError(f"Solicitation unavailable ({type(error).__name__}).") from error


def validate_solicitation_draft(program_id: str, draft: Path, *, programs_root: Path) -> bool:
    """Check new packet bindings; legacy drafts remain explicitly unknown-bound."""
    try:
        program = _validate_paths(program_id, programs_root)
        if not draft.resolve().is_relative_to(program):
            raise ContextGapSolicitationError("Draft leaves the selected program.")
        raw = draft.read_bytes()
        message = message_from_bytes(raw)
        marker = str(message.get("X-Vertex-Solicitation", ""))
        if not draft.stem.startswith(_PACKET_PREFIX) and not marker:
            return False
        db = get_program_reality_db_path(program_id, db_root=programs_root.parent)
        if any(
            not path.resolve().is_relative_to(programs_root.parent.resolve())
            for path in (db, *(db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal")))
        ):
            raise ContextGapSolicitationError("Solicitation audit storage leaves the selected workspace.")
        records = _read_records(program / "_feedback" / "context_gap_solicitations.jsonl")
        associated = [row for row in records if row.get("solicitation_id") == draft.stem]
        if len(associated) != 1 or marker != draft.stem:
            raise ContextGapSolicitationError("Missing or ambiguous solicitation packet binding.")
        row = associated[0]
        if (
            type(row.get("packet_version")) is not int or row["packet_version"] != 1
            or not isinstance(row.get("target_key"), str) or not row["target_key"].strip()
            or not isinstance(row.get("target_field"), str)
            or not isinstance(row.get("current_value"), str)
            or not isinstance(row.get("current_value_hash"), str)
            or re.fullmatch(r"intake-v1:[0-9a-f]{64}", row["current_value_hash"]) is None
            or not isinstance(row.get("eml_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", row["eml_sha256"]) is None
        ):
            raise ContextGapSolicitationError("Malformed solicitation packet binding.")
        binding_parts: tuple[str | None, ...] = (
            program_id, row.get("target_key"), row.get("target_field"),
            row.get("current_value"), row.get("current_value_hash"),
        )
        detector_fingerprint = f"{program_id}:doctor --context:{row.get('target_key')}:{row.get('target_field')}"
        if row.get("gap_fingerprint") == detector_fingerprint:
            binding_parts += (detector_fingerprint,)
        binding = json.dumps(binding_parts, ensure_ascii=False)
        fingerprints = {
            f"{program_id}:workstream_registry:{row.get('target_key')}:{row.get('target_field')}",
        }
        if row.get("target_field") in {"deep_context.why", "deep_context.what"}:
            fingerprints.add(detector_fingerprint)
        if (
            row.get("program_id") != program_id
            or row.get("target_store") != "workstream_registry"
            or row.get("target_field") not in {"deep_context.why", "deep_context.what", "deep_context.how"}
            or row.get("gap_fingerprint") not in fingerprints
            or row.get("eml_sha256") != sha256(raw).hexdigest()
            or message.get("X-Vertex-Packet-Binding") != sha256(binding.encode("utf-8")).hexdigest()
        ):
            raise ContextGapSolicitationError("Solicitation packet content or target binding changed.")
        current = read_registry_baseline(
            program_id, row["target_key"], row["target_field"], programs_root=programs_root,
        )
        if current != (row.get("current_value"), row.get("current_value_hash")):
            raise ContextGapSolicitationError("Solicitation packet is stale: its target baseline changed.")
        return True
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        if isinstance(error, ContextGapSolicitationError):
            raise
        raise ContextGapSolicitationError(f"Solicitation binding unavailable ({type(error).__name__}).") from error


__all__ = [
    "ContextGapSolicitation",
    "ContextGapSolicitationError",
    "generate_deterministic_solicitation",
    "is_in_cooldown",
    "record_solicitation_drafted",
    "produce_context_gap_solicitation",
    "validate_solicitation_draft",
    "write_solicitation_draft",
]
