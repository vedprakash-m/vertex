"""REV LocalFileEnumerator (Zone C) — FR-PCI-2, Phase 3 (P3-5).

specs/gaps.md P3-5. Enumerates locally-downloaded document files (.docx, .pdf)
from a docs inbox directory using the shared 3-directory atomicity model
(``LocalInboxClaimer``). The canonical dedup key is a content-addressed
SHA-256 hash of the file bytes.

Two ``LocalInboxClaimer`` instances share the same inbox root — one claiming
``*.docx``, one claiming ``*.pdf`` — called sequentially so the shared
``cycle.lock`` serializes them correctly. Results are merged and truncated to
the caller's limit (docx first, then pdf, each in FIFO mtime order).

Inbox root convention: ``programs/{program_id}/rev_docs_inbox/``.

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, PortResult, Success, Unsupported
from src.m365.rev.local_inbox import LocalInboxClaimer, DEFAULT_MAX_BYTES

log = logging.getLogger(__name__)

_DOC_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file (oversized → quarantine)
_DOCX_GLOB = "*.docx"
_PDF_GLOB = "*.pdf"


def _file_sha256_id(path: Path) -> str:
    """Content-addressed logical ID: sha256 of file bytes, truncated to 48 hex chars.

    Stable across renames; identical files collapse to the same ID. Falls back
    to a path-derived hash if the file cannot be read.
    """
    try:
        data = path.read_bytes()
        return "sha256:" + hashlib.sha256(data).hexdigest()[:48]
    except OSError:
        return "sha256:" + hashlib.sha256(path.name.encode()).hexdigest()[:48]


class LocalFileEnumerator:
    """Enumerate locally-downloaded ``.docx`` and ``.pdf`` files for the REV pipeline.

    Satisfies the ``CandidateEnumerator`` Protocol. Two underlying
    ``LocalInboxClaimer`` instances share the same inbox root directory, each
    handling one file type. They are called sequentially; the shared
    ``claimed/cycle.lock`` serializes them correctly across concurrent processes.

    Inbox root convention: ``programs/{program_id}/rev_docs_inbox/``.

    Usage::

        enumerator = LocalFileEnumerator(
            inbox_root=Path("programs/{program_id}/rev_docs_inbox"),
            mailbox_tenant_id="tenant-abc",
            principal_mailbox="tpm@example.com",
        )
        result = enumerator.enumerate(intent, correlation_id="rev-cycle-docs-001")
    """

    entity_type = EntityType.DRIVE_ITEM

    def __init__(
        self,
        *,
        inbox_root: Path,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "docs",
        limit: int | None = None,
    ) -> None:
        self._limit = limit
        common = dict(
            inbox_root=inbox_root,
            logical_id_fn=_file_sha256_id,
            entity_type=EntityType.DRIVE_ITEM,
            enumerator_name="local_file",
            path_metadata_key="file_path",
            logical_id_metadata_key="file_sha256",
            mailbox_tenant_id=mailbox_tenant_id,
            principal_mailbox=principal_mailbox,
            container=container,
            max_bytes=_DOC_MAX_BYTES,
        )
        # Claimers share the same inbox root (and thus the same cycle.lock).
        # They are called sequentially in enumerate() — no lock contention.
        self._claimer_docx = LocalInboxClaimer(
            glob_pattern=_DOCX_GLOB,
            limit=limit,
            **common,  # type: ignore[arg-type]
        )
        self._claimer_pdf = LocalInboxClaimer(
            glob_pattern=_PDF_GLOB,
            limit=limit,
            **common,  # type: ignore[arg-type]
        )

    @property
    def claimed_at_startup_count(self) -> int:
        return (
            self._claimer_docx.claimed_at_startup_count
            + self._claimer_pdf.claimed_at_startup_count
        )

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        """Enumerate .docx + .pdf files from inbox/ + claimed/ (crash recovery)."""
        effective_limit = self._limit or getattr(intent, "limit", None) or 50

        # Enumerate docx files first (cycle.lock acquired + released).
        docx_result = self._claimer_docx.enumerate(intent, correlation_id=correlation_id)
        # Enumerate pdf files next (cycle.lock acquired again sequentially).
        pdf_result = self._claimer_pdf.enumerate(intent, correlation_id=correlation_id)

        if isinstance(docx_result, Unsupported) and isinstance(pdf_result, Unsupported):
            return Unsupported(
                entity_type=EntityType.DRIVE_ITEM.value,
                reason=f"both_surfaces_unavailable: docx={docx_result.reason}; pdf={pdf_result.reason}",
            )

        # Merge candidates from both surfaces.
        docx_items: tuple[EnumeratedCandidate, ...] = (
            docx_result.value if hasattr(docx_result, "value") else ()
        )
        pdf_items: tuple[EnumeratedCandidate, ...] = (
            pdf_result.value if hasattr(pdf_result, "value") else ()
        )
        merged = (docx_items + pdf_items)[:effective_limit]

        was_truncated = (
            isinstance(docx_result, Incomplete)
            or isinstance(pdf_result, Incomplete)
            or len(docx_items) + len(pdf_items) > effective_limit
        )

        if was_truncated:
            return Incomplete(
                value=merged,
                reason=f"budget_stop: limit={effective_limit}",
            )
        return Success(merged)

    def mark_processed(self, path: str | Path) -> None:
        """Move a claimed file to processed/. Works for both docx and pdf."""
        self._claimer_docx.mark_processed(path)

    def mark_quarantined(self, path: str | Path, *, reason: str) -> None:
        """Move a claimed file to quarantine/. Works for both docx and pdf."""
        self._claimer_docx.mark_quarantined(path, reason=reason)

    def claimed_dir(self) -> Path:
        return self._claimer_docx.claimed_dir()

    def processed_dir(self) -> Path:
        return self._claimer_docx.processed_dir()

    def quarantine_dir(self) -> Path:
        return self._claimer_docx.quarantine_dir()

    def count_quarantine_files(self) -> int:
        qdir = self.quarantine_dir()
        if not qdir.exists():
            return 0
        # Count both docx and pdf files in quarantine (plus any reason.txt stubs).
        return sum(
            1 for p in qdir.iterdir()
            if p.suffix.lower() in (".docx", ".pdf")
        )


__all__ = ["LocalFileEnumerator", "_file_sha256_id"]
