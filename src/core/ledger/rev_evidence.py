"""REV evidence model + two-stage excerpt vaulting (Zone A).

specs/program-context-intelligence.md §5.7. There is **one** evidence type,
``EvidenceRef``, identifying a *supporting excerpt* within a vaulted body. The
candidate's ledger ``SourceRef`` (``src/core/ledger/source_refs.py``) identifies
the *source item*; ``EvidenceRef`` identifies the excerpt. A candidate carries
``evidence_refs: tuple[EvidenceRef, ...]`` (≥1 for M365+AI-extracted).

Two-stage lifecycle (§5.7 Stage 1 ephemeral → Stage 2 persist): extraction
identifies spans in transient chunks first; only *admitted excerpts* (span +
minimal context) are vaulted here. The vaulted excerpt is stored via the
existing content-addressed ``store_evidence_vault_bytes`` and a **versioned REV
metadata sidecar** is written alongside it (the §5.7 schema). Retention is
**by reference state**, not age (QG-DM-4/8/12).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ledger.evidence_vault import (
    PROGRAMS_ROOT as _VAULT_PROGRAMS_ROOT,
    delete_evidence_vault_entry,
    evidence_vault_entry_status,
    evidence_vault_paths,
    store_evidence_vault_bytes,
)


# Current evidence metadata schema version — bump on any field addition.
REV_EVIDENCE_METADATA_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A supporting excerpt within a vaulted body (§5.7).

    Spans are codepoints into the **canonical normalized text** (post
    HTML/MIME strip, post quoted-reply removal, post PII scrub), not the
    original bytes. ``vault_hash`` links to the vaulted normalized excerpt;
    ``excerpt_hash`` / ``normalized_source_hash`` are integrity hashes.
    """

    vault_hash: str
    representation_version: str    # normalization pipeline version
    start_codepoint: int
    end_codepoint: int
    excerpt_hash: str              # SHA-256 of the excerpt text
    normalized_source_hash: str    # SHA-256 of the full canonical source

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_hash": self.vault_hash,
            "representation_version": self.representation_version,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
            "excerpt_hash": self.excerpt_hash,
            "normalized_source_hash": self.normalized_source_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvidenceRef":
        return cls(
            vault_hash=str(payload["vault_hash"]),
            representation_version=str(payload["representation_version"]),
            start_codepoint=int(payload["start_codepoint"]),
            end_codepoint=int(payload["end_codepoint"]),
            excerpt_hash=str(payload["excerpt_hash"]),
            normalized_source_hash=str(payload["normalized_source_hash"]),
        )


@dataclass(frozen=True, slots=True)
class RevEvidenceMetadata:
    """Versioned evidence metadata stored with the vault entry (§5.7 schema)."""

    schema_version: str = REV_EVIDENCE_METADATA_SCHEMA_VERSION
    tenant_id_hash: str = ""
    principal_mailbox_container_hash: str = ""
    canonical_item_id: str = ""
    canonical_route_id: str | None = None
    native_etag: str | None = None
    native_change_key: str | None = None
    retrieval_timestamp: datetime | None = None
    normalization_version: str = ""
    scrubber_version: str = ""
    injection_policy_version: str = ""
    prompt_version: str = ""
    extraction_policy_version: str = ""
    chunking_version: str = ""
    extraction_model: str = ""
    extraction_schema_version: str = ""
    content_safety_result: str | None = None      # "pass" | "flagged" | "unavailable"
    content_safety_policy_version: str | None = None
    human_materiality_policy_version: str | None = None
    source_classification: str | None = None
    sensitivity_label: str | None = None
    retention_class: str = "unreferenced"          # see retention-by-reference table
    purge_deadline: date | None = None             # None for accepted-event evidence
    resulting_event_id: str | None = None          # filled after triage acceptance

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "tenant_id_hash": self.tenant_id_hash,
            "principal_mailbox_container_hash": self.principal_mailbox_container_hash,
            "canonical_item_id": self.canonical_item_id,
            "canonical_route_id": self.canonical_route_id,
            "native_etag": self.native_etag,
            "native_change_key": self.native_change_key,
            "retrieval_timestamp": self.retrieval_timestamp.isoformat() if self.retrieval_timestamp else None,
            "normalization_version": self.normalization_version,
            "scrubber_version": self.scrubber_version,
            "injection_policy_version": self.injection_policy_version,
            "prompt_version": self.prompt_version,
            "extraction_policy_version": self.extraction_policy_version,
            "chunking_version": self.chunking_version,
            "extraction_model": self.extraction_model,
            "extraction_schema_version": self.extraction_schema_version,
            "content_safety_result": self.content_safety_result,
            "content_safety_policy_version": self.content_safety_policy_version,
            "human_materiality_policy_version": self.human_materiality_policy_version,
            "source_classification": self.source_classification,
            "sensitivity_label": self.sensitivity_label,
            "retention_class": self.retention_class,
            "purge_deadline": self.purge_deadline.isoformat() if self.purge_deadline else None,
            "resulting_event_id": self.resulting_event_id,
        }
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RevEvidenceMetadata":
        retrieval = payload.get("retrieval_timestamp")
        purge = payload.get("purge_deadline")
        return cls(
            schema_version=str(payload.get("schema_version", REV_EVIDENCE_METADATA_SCHEMA_VERSION)),
            tenant_id_hash=str(payload.get("tenant_id_hash", "")),
            principal_mailbox_container_hash=str(payload.get("principal_mailbox_container_hash", "")),
            canonical_item_id=str(payload.get("canonical_item_id", "")),
            canonical_route_id=payload.get("canonical_route_id"),
            native_etag=payload.get("native_etag"),
            native_change_key=payload.get("native_change_key"),
            retrieval_timestamp=datetime.fromisoformat(retrieval) if isinstance(retrieval, str) else None,
            normalization_version=str(payload.get("normalization_version", "")),
            scrubber_version=str(payload.get("scrubber_version", "")),
            injection_policy_version=str(payload.get("injection_policy_version", "")),
            prompt_version=str(payload.get("prompt_version", "")),
            extraction_policy_version=str(payload.get("extraction_policy_version", "")),
            chunking_version=str(payload.get("chunking_version", "")),
            extraction_model=str(payload.get("extraction_model", "")),
            extraction_schema_version=str(payload.get("extraction_schema_version", "")),
            content_safety_result=payload.get("content_safety_result"),
            content_safety_policy_version=payload.get("content_safety_policy_version"),
            human_materiality_policy_version=payload.get("human_materiality_policy_version"),
            source_classification=payload.get("source_classification"),
            sensitivity_label=payload.get("sensitivity_label"),
            retention_class=str(payload.get("retention_class", "unreferenced")),
            purge_deadline=date.fromisoformat(purge) if isinstance(purge, str) else None,
            resulting_event_id=payload.get("resulting_event_id"),
        )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tenant_hash(tenant_id: str) -> str:
    return "sha256:" + _sha256_hex(("tenant:" + tenant_id).encode("utf-8"))[:32]


def _mailbox_container_hash(tenant_id: str, principal_mailbox: str, container: str) -> str:
    raw = f"mbox:{tenant_id}|{principal_mailbox}|{container}"
    return "sha256:" + _sha256_hex(raw.encode("utf-8"))[:32]


def _revmeta_path(content_path: Path) -> Path:
    return content_path.with_name(content_path.name + ".revmeta.json")


def store_admitted_excerpt(
    *,
    program_id: str,
    excerpt_text: str,
    normalized_source_text: str,
    metadata: RevEvidenceMetadata,
    programs_root: Path = _VAULT_PROGRAMS_ROOT,
) -> EvidenceRef:
    """Vault one admitted excerpt + write its REV metadata sidecar (§5.7 Stage 2).

    Returns the ``EvidenceRef`` to attach to the candidate. The excerpt bytes
    are the canonical normalized excerpt (post-scrub); the normalized source
    text is the parent body used for the ``normalized_source_hash`` integrity
    hash and span validation.
    """
    excerpt_bytes = excerpt_text.encode("utf-8")
    entry = store_evidence_vault_bytes(
        program_id=program_id,
        content_bytes=excerpt_bytes,
        content_type="text/plain",
        original_filename=f"{metadata.canonical_item_id or 'excerpt'}.txt",
        origin_path=None,
        programs_root=programs_root,
    )
    content_path, _meta_path = evidence_vault_paths(
        program_id=program_id, vault_hash=entry.vault_hash, programs_root=programs_root,
    )
    revmeta_path = _revmeta_path(content_path)
    revmeta_path.parent.mkdir(parents=True, exist_ok=True)
    revmeta_path.write_text(
        json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return EvidenceRef(
        vault_hash=entry.vault_hash,
        representation_version=metadata.normalization_version,
        start_codepoint=0,
        end_codepoint=len(excerpt_text),
        excerpt_hash="sha256:" + _sha256_hex(excerpt_bytes),
        normalized_source_hash="sha256:" + _sha256_hex(normalized_source_text.encode("utf-8")),
    )


def load_rev_evidence_metadata(
    *,
    program_id: str,
    vault_hash: str,
    programs_root: Path = _VAULT_PROGRAMS_ROOT,
) -> RevEvidenceMetadata | None:
    content_path, _ = evidence_vault_paths(
        program_id=program_id, vault_hash=vault_hash, programs_root=programs_root,
    )
    revmeta_path = _revmeta_path(content_path)
    if not revmeta_path.exists():
        return None
    payload = json.loads(revmeta_path.read_text(encoding="utf-8"))
    return RevEvidenceMetadata.from_dict(payload)


def read_excerpt_text(
    *,
    program_id: str,
    vault_hash: str,
    programs_root: Path = _VAULT_PROGRAMS_ROOT,
) -> str | None:
    """Read the vaulted excerpt text for span validation (§5.9 quote_span check)."""
    status = evidence_vault_entry_status(program_id=program_id, vault_hash=vault_hash, programs_root=programs_root)
    if status != "ok":
        return None
    content_path, _ = evidence_vault_paths(
        program_id=program_id, vault_hash=vault_hash, programs_root=programs_root,
    )
    if not content_path.exists():
        return None
    return content_path.read_text(encoding="utf-8")


def evidence_refs_to_dict(refs: tuple[EvidenceRef, ...]) -> list[dict[str, Any]]:
    return [ref.to_dict() for ref in refs]


def evidence_refs_from_dict(payload: Any) -> tuple[EvidenceRef, ...]:
    if not isinstance(payload, list | tuple):
        return ()
    return tuple(EvidenceRef.from_dict(item) for item in payload)


# --- Retention by reference state (§5.7) ---

RETENTION_CLASS_UNREFERENCED = "unreferenced"
RETENTION_CLASS_REJECTED = "rejected"
RETENTION_CLASS_PENDING = "pending"
RETENTION_CLASS_ACCEPTED_EVENT = "accepted_event"


def retention_class_for(
    *,
    has_candidate: bool,
    has_assertion: bool,
    decision_kind: str | None,
    resulting_event_id: str | None,
) -> str:
    """Classify an excerpt's retention tier (§5.7 retention-by-reference table).

    Accepted-event evidence is retained for the ledger's governed retention and
    is NOT purged by this pipeline. Rejected/pending/unreferenced tiers use the
    configured review/grace/orphan TTLs.
    """
    if resulting_event_id is not None:
        return RETENTION_CLASS_ACCEPTED_EVENT
    if decision_kind == "rejected":
        return RETENTION_CLASS_REJECTED
    if has_candidate or has_assertion:
        return RETENTION_CLASS_PENDING
    return RETENTION_CLASS_UNREFERENCED


def compute_purge_deadline(
    *,
    retention_class: str,
    as_of: datetime,
    orphan_ttl_days: int = 7,
    rejected_review_retention_days: int = 30,
    pending_grace_days: int = 14,
) -> date | None:
    """When an excerpt may be purged, or ``None`` for accepted-event evidence."""
    if retention_class == RETENTION_CLASS_ACCEPTED_EVENT:
        return None  # ledger-governed; never purged by this pipeline
    if retention_class == RETENTION_CLASS_REJECTED:
        return (as_of + _days(rejected_review_retention_days)).date()
    if retention_class == RETENTION_CLASS_PENDING:
        return (as_of + _days(pending_grace_days)).date()
    return (as_of + _days(orphan_ttl_days)).date()


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def find_orphan_vault_hashes(
    program_id: str,
    *,
    referenced_vault_hashes: frozenset[str],
    active_run_vault_hashes: frozenset[str],
    as_of: datetime | None = None,
    orphan_ttl_days: int = 7,
    programs_root: Path = _VAULT_PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """Vault hashes safe to purge as orphans (§5.7 / §5.10).

    An excerpt is an orphan only if it is **unreferenced** (no candidate,
    no VerificationAssertion, no accepted event) AND not in an active run state
    AND past ``orphan_ttl_days``. Accepted-event evidence is never returned
    here — it is governed by QG-DM-4 redaction, not ordinary cleanup.
    """
    from src.core.ledger.evidence_vault import load_evidence_vault_entries

    now = as_of or datetime.now(timezone.utc)
    cutoff = now - _days(orphan_ttl_days)
    orphans: list[str] = []
    for entry in load_evidence_vault_entries(program_id, programs_root=programs_root):
        vault_hash = entry.vault_hash
        if vault_hash in referenced_vault_hashes:
            continue
        if vault_hash in active_run_vault_hashes:
            continue  # mid-cycle; protected even if temporarily unreferenced
        meta = load_rev_evidence_metadata(
            program_id=program_id, vault_hash=vault_hash, programs_root=programs_root,
        )
        if meta is not None and meta.retention_class == RETENTION_CLASS_ACCEPTED_EVENT:
            continue  # ledger-governed; never orphan-purged
        if meta is not None and meta.retrieval_timestamp is not None:
            if meta.retrieval_timestamp > cutoff:
                continue  # too recent
        orphans.append(vault_hash)
    return tuple(orphans)


def purge_orphan_excerpts(
    program_id: str,
    *,
    referenced_vault_hashes: frozenset[str],
    active_run_vault_hashes: frozenset[str],
    as_of: datetime | None = None,
    orphan_ttl_days: int = 7,
    programs_root: Path = _VAULT_PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """Delete orphan vault excerpts + their REV metadata sidecars. Returns purged hashes."""
    purged: list[str] = []
    for vault_hash in find_orphan_vault_hashes(
        program_id,
        referenced_vault_hashes=referenced_vault_hashes,
        active_run_vault_hashes=active_run_vault_hashes,
        as_of=as_of,
        orphan_ttl_days=orphan_ttl_days,
        programs_root=programs_root,
    ):
        # Remove the REV metadata sidecar first, then the vault entry cascade.
        content_path, _ = evidence_vault_paths(
            program_id=program_id, vault_hash=vault_hash, programs_root=programs_root,
        )
        revmeta_path = _revmeta_path(content_path)
        if revmeta_path.exists():
            revmeta_path.unlink()
        if delete_evidence_vault_entry(
            program_id=program_id, vault_hash=vault_hash, programs_root=programs_root,
        ) is not None:
            purged.append(vault_hash)
    return tuple(purged)


def build_metadata_defaults(
    *,
    tenant_id: str,
    principal_mailbox: str,
    container: str,
    canonical_item_id: str,
    canonical_route_id: str | None,
    retrieval_timestamp: datetime,
    profile: Any,
    extraction_model: str = "",
    extraction_schema_version: str = "",
    prompt_version: str = "",
    content_safety_result: str | None = None,
) -> RevEvidenceMetadata:
    """Build a ``RevEvidenceMetadata`` from a REV profile + identity (§5.7)."""
    return RevEvidenceMetadata(
        schema_version=REV_EVIDENCE_METADATA_SCHEMA_VERSION,
        tenant_id_hash=_tenant_hash(tenant_id),
        principal_mailbox_container_hash=_mailbox_container_hash(tenant_id, principal_mailbox, container),
        canonical_item_id=canonical_item_id,
        canonical_route_id=canonical_route_id,
        retrieval_timestamp=retrieval_timestamp,
        normalization_version=getattr(profile, "normalization_version", ""),
        scrubber_version=getattr(profile, "scrubber_version", ""),
        injection_policy_version=getattr(profile, "injection_policy_version", ""),
        prompt_version=prompt_version,
        extraction_policy_version=getattr(profile, "extraction_policy_version", ""),
        chunking_version=getattr(profile, "chunking_version", ""),
        extraction_model=extraction_model,
        extraction_schema_version=extraction_schema_version,
        content_safety_result=content_safety_result,
        content_safety_policy_version=getattr(profile, "content_safety_policy_version", None),
        human_materiality_policy_version=getattr(profile, "human_materiality_policy_version", None),
        retention_class=RETENTION_CLASS_PENDING,
        purge_deadline=None,
        resulting_event_id=None,
    )