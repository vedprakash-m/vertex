"""PII-scrubbed REV corpus export (P2-5).

``vertex rev export-corpus --program {program_id} --output <path>`` produces a
PII-scrubbed backup bundle of a program's REV labeled corpus + staged
candidates + triage decisions (+ optionally the vaulted evidence excerpts) so
the quality-floor corpus can be backed up, restored, or re-measured without
leaking direct identifiers.

**Redaction policy (direct identifiers only):**
- SMTP addresses (``sender``, ``principal_mailbox``, triage ``triage_actor``)
  and correlatable message-ids are replaced with ``redacted:<sha256[:12]>``.
  A display name in ``"Name <addr>"`` form is preserved as
  ``"Name <redacted:…>"`` (matches the ICS surface's "display name only, no raw
  SMTP" rule).
- Content-derived hashes (``vault_hash``, ``candidate_id``, ``dedupe_key``,
  ``dedupe_core_hash``, ``source_document_key``, ``excerpt_hash``) are **kept**:
  they are not PII and are required for restore / dedup / drift detection.
- Content fields (``subject``, ``proposed_payload``, excerpt text) are **kept**
  because they are the corpus's analytic value. The manifest records a warning
  that content fields may contain incidental PII, so the export is intended for
  operator-controlled backup (self-containment directive), not public sharing.

Zone A — no AI or M365 imports. Reads via the sanctioned ledger loaders.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ledger.candidate_store import (
    get_candidate_dir,
    load_pending_candidates,
    load_triage_decisions,
)
from src.core.jsonl_utils import read_jsonl_records

log = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION = "rev_corpus_export.v1"
_REDACT_PREFIX = "redacted:"


def _hash_redact(value: str | None) -> str | None:
    """Replace an identifier with ``redacted:<sha256[:12]>``; None passes through."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{_REDACT_PREFIX}{digest}"


def redact_email(value: str | None) -> str | None:
    """Redact an SMTP address, preserving a ``"Name <addr>"`` display name.

    ``"Owner <owner@example.com>"`` → ``"Owner <redacted:abc123def456>"``.
    Bare addresses → ``redacted:<hash>``. None passes through.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return value
    # Display-name + angle-bracket address form.
    if "<" in value and value.strip().endswith(">"):
        head, _, rest = value.partition("<")
        addr, _, _ = rest.partition(">")
        if addr:
            return f"{head}<{_hash_redact(addr)}>" if head.strip() else f"<{_hash_redact(addr)}>"
    return _hash_redact(value)


def _scrub_source_ref(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(ref, dict):
        return ref
    out = dict(ref)
    if out.get("ref_type") == "email":
        out["sender"] = redact_email(out.get("sender"))
        out["message_id"] = _hash_redact(out.get("message_id"))
    # vault_hash / folder kept (folder is a mailbox path, low-sensitivity; keep
    # for provenance — operators may redact further if their container name is
    # sensitive).
    return out


def _scrub_metadata(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    for key in ("tenant_id", "principal_mailbox", "canonical_item_id", "canonical_route_id"):
        if key in out:
            out[key] = _hash_redact(out.get(key))
    return out


def _candidate_to_scrubbed_record(candidate: Any) -> dict[str, Any]:
    """Serialize a CandidateEvent to a dict with direct identifiers redacted."""
    source_ref = getattr(candidate, "source_ref", None)
    rec = {
        "candidate_id": candidate.candidate_id,
        "program_id": candidate.program_id,
        "proposed_event_type": candidate.proposed_event_type,
        "proposed_payload": dict(candidate.proposed_payload or {}),
        "proposed_occurred_at": candidate.proposed_occurred_at.isoformat()
        if candidate.proposed_occurred_at else None,
        "proposed_temporal_confidence": candidate.proposed_temporal_confidence,
        "proposed_confidence": candidate.proposed_confidence,
        "pipeline": candidate.pipeline,
        "extraction_confidence": candidate.extraction_confidence,
        "dedupe_key": candidate.dedupe_key,
        "dedupe_core_hash": candidate.dedupe_core_hash,
        "source_document_key": candidate.source_document_key,
        "batch_id": candidate.batch_id,
        "schema_version": getattr(candidate, "schema_version", "1"),
        "source_ref": _scrub_source_ref(_ref_to_dict(source_ref)),
        "evidence_refs": [
            _ref_to_dict(e) for e in (getattr(candidate, "evidence_refs", ()) or ())
        ],
    }
    return rec


def _ref_to_dict(ref: Any) -> dict[str, Any] | None:
    if ref is None:
        return None
    if isinstance(ref, dict):
        return ref
    # SourceRef / EvidenceRef dataclasses expose to_dict() in some classes;
    # fall back to attribute harvest.
    if hasattr(ref, "to_dict"):
        try:
            return ref.to_dict()  # type: ignore[no-any-return]
        except TypeError:
            pass
    out: dict[str, Any] = {}
    for attr in ("ref_type", "vault_hash", "subject", "sent_at", "sender",
                 "message_id", "folder", "representation_version",
                 "start_codepoint", "end_codepoint", "excerpt_hash",
                 "normalized_source_hash"):
        if hasattr(ref, attr):
            val = getattr(ref, attr)
            if isinstance(val, datetime):
                val = val.isoformat()
            out[attr] = val
    return out or None


def export_corpus(
    *,
    program_id: str,
    output_dir: Path,
    programs_root: Path,
    include_vault: bool = False,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    """Export a PII-scrubbed REV corpus bundle to ``output_dir``.

    Writes ``candidates.jsonl``, ``triage_decisions.jsonl``, the labeled corpus
    copy (if present), optionally ``evidence_vault.jsonl``, and a ``manifest.json``.
    Returns the manifest dict.
    """
    from src.core.ledger.rev_evidence import (
        load_rev_evidence_metadata,
        read_excerpt_text,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = (exported_at or datetime.now(timezone.utc)).isoformat()

    warnings: list[str] = []
    counts: dict[str, int] = {}

    # 1. Candidates (PII-scrubbed).
    candidates = load_pending_candidates(program_id, programs_root=programs_root)
    cand_records = [_candidate_to_scrubbed_record(c) for c in candidates]
    _write_jsonl(output_dir / "candidates.jsonl", cand_records)
    counts["candidates"] = len(cand_records)

    # 2. Triage decisions (scrub the actor identity).
    decisions = load_triage_decisions(program_id, programs_root=programs_root)
    dec_records: list[dict[str, Any]] = []
    for d in decisions:
        suppress_until = getattr(d, "suppress_until", None)
        dec_records.append({
            "candidate_id": d.candidate_id,
            "kind": d.kind,
            "decided_at": d.decided_at.isoformat() if getattr(d, "decided_at", None) else None,
            "triage_actor": _hash_redact(getattr(d, "triage_actor", None)),
            "batch_id": getattr(d, "batch_id", None),
            "reason": getattr(d, "reason", None),
            "edited": getattr(d, "edited", None),
            "resulting_event_id": getattr(d, "resulting_event_id", None),
            "suppress_until": suppress_until.isoformat() if isinstance(suppress_until, datetime) else None,
            "gap_event_id": getattr(d, "gap_event_id", None),
        })
    _write_jsonl(output_dir / "triage_decisions.jsonl", dec_records)
    counts["triage_decisions"] = len(dec_records)

    # 3. Labeled corpus (copy through if present; records are operator-authored
    # and may already reference candidate_ids / hashes — pass through, the
    # operator is responsible for the label schema).
    corpus_path = programs_root / program_id / "_quality" / "rev_labeled_corpus.jsonl"
    corpus_present = corpus_path.exists()
    counts["labeled_corpus_records"] = 0
    if corpus_present:
        records = read_jsonl_records(corpus_path)
        _write_jsonl(output_dir / "rev_labeled_corpus.jsonl", list(records))
        counts["labeled_corpus_records"] = len(records)
    else:
        warnings.append("rev_labeled_corpus.jsonl absent — no labeled corpus to export (OA-3 not yet run).")

    # 4. Evidence vault (optional — excerpt text may contain incidental PII).
    counts["evidence_excerpts"] = 0
    if include_vault:
        vault_records: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for c in candidates:
            for ref in (getattr(c, "evidence_refs", ()) or ()):
                vh = getattr(ref, "vault_hash", None)
                if not vh or vh in seen_hashes:
                    continue
                seen_hashes.add(vh)
                text = read_excerpt_text(program_id=program_id, vault_hash=vh, programs_root=programs_root)
                meta = load_rev_evidence_metadata(program_id=program_id, vault_hash=vh, programs_root=programs_root)
                vault_records.append({
                    "vault_hash": vh,
                    "excerpt_hash": getattr(ref, "excerpt_hash", None),
                    "normalized_source_hash": getattr(ref, "normalized_source_hash", None),
                    "excerpt_text": text,
                    "metadata": _scrub_metadata(meta.to_dict() if meta is not None else None),
                })
        _write_jsonl(output_dir / "evidence_vault.jsonl", vault_records)
        counts["evidence_excerpts"] = len(vault_records)
        warnings.append(
            "evidence_vault.jsonl includes raw excerpt text — may contain incidental PII. "
            "Operator-controlled backup only; do not share externally."
        )

    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "program_id": program_id,
        "exported_at": stamp,
        "redaction_policy": {
            "direct_identifiers": "sha256[:12] hash (sender SMTP, message_id, principal_mailbox, tenant_id, triage_actor)",
            "display_names": "preserved (Name <redacted:hash>)",
            "content_hashes": "kept (vault_hash, candidate_id, dedupe_*, source_document_key, excerpt_hash)",
            "content_fields": "kept (subject, proposed_payload, excerpt_text) — may contain incidental PII",
        },
        "counts": counts,
        "includes_vault": include_vault,
        "warnings": warnings,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records),
        encoding="utf-8",
    )


__all__ = [
    "export_corpus",
    "redact_email",
    "_hash_redact",
    "EXPORT_SCHEMA_VERSION",
]