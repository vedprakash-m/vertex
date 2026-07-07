"""REV EmlEnumerator (Zone C) — FR-PCI-2, Phase 1.

specs/gaps.md P1-1. Enumerates locally-exported ``.eml`` files from an
``inbox/`` directory, implementing the 3-directory atomicity model:

    inbox/   — drop zone (user copies .eml files here)
    claimed/ — in-flight (atomically renamed before processing; replayed on crash)
    processed/ — completed (moved after EmlHydrator succeeds)

**Crash recovery:** files found in ``claimed/`` at startup are prior-crash
in-flight items. The enumerator surfaces them first (at the front of the FIFO
queue) so the pipeline re-hydrates them on the next run.

**FIFO ordering:** primary sort key = ``mtime``; secondary sort key =
``SHA-256(message_id)[:8]`` for batch-drop tie-breaking. This ensures
deterministic ordering even when multiple files land in the same second.

**Missing Message-ID fallback:** if an ``.eml`` has no ``Message-ID`` header,
the logical ID is ``sha256:<hex>`` derived from
``MIME-From || MIME-Subject || MIME-Date``. This is stable across re-imports
of the same message.

**Concurrency guard:** a ``portalocker``-based ``cycle.lock`` in the
``claimed/`` dir prevents two concurrent ``vertex rev run`` invocations from
claiming the same file.

**Network drive OSError fallback:** if ``os.rename`` across filesystems raises
``OSError`` (``EXDEV``), falls back to ``shutil.copy2 + unlink`` with an
``fsync`` on the destination before unlinking the source, so the copy is durable
before the original is removed.

Zone C: imports only ``src.core.*`` + stdlib + portalocker.
"""

from __future__ import annotations

import email
import email.policy
import logging
from pathlib import Path

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import PortResult
from src.m365.rev.local_inbox import LocalInboxClaimer, sha256_hex

log = logging.getLogger(__name__)

_EML_GLOB = "*.eml"
_MAX_EML_BYTES = 10 * 1024 * 1024   # 10 MB per .eml (oversized → quarantine)


def _message_id_from_eml(path: Path) -> str:
    """Return Message-ID header value, or synthesize one from stable fields."""
    try:
        with path.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.compat32)
        mid = msg.get("Message-ID", "").strip()
        if mid:
            return mid
        # Fallback: stable hash from From + Subject + Date
        from_hdr = msg.get("From", "")
        subject_hdr = msg.get("Subject", "")
        date_hdr = msg.get("Date", "")
        seed = f"{from_hdr}||{subject_hdr}||{date_hdr}"
        return f"sha256:{sha256_hex(seed)[:32]}"
    except Exception:
        # Absolute last resort — use the file stem
        return f"sha256:{sha256_hex(path.stem)[:32]}"


class EmlEnumerator:
    """Enumerate locally-exported ``.eml`` files for the REV pipeline (Phase 1).

    Satisfies the ``CandidateEnumerator`` Protocol (Zone A ``src/core/rev/ports.py``).
    The ``RetrievalIntent`` is used for ``tenant_id`` / ``principal_mailbox`` /
    ``container`` metadata only; the EML content determines the actual candidates.

    The 3-directory atomicity, FIFO ordering, crash-loop guard, concurrency lock,
    and size guard are delegated to the shared ``LocalInboxClaimer``
    (``src/m365/rev/local_inbox.py``) so every local-import surface (EML, ICS,
    Teams, docs) shares one atomicity implementation.

    Usage::

        enumerator = EmlEnumerator(
            inbox_root=Path("programs/{program_id}/rev_inbox"),
            mailbox_tenant_id="tenant-abc",
            principal_mailbox="tpm@example.com",
        )
        result = enumerator.enumerate(intent, correlation_id="rev-cycle-001")
        # Returns Success(tuple[EnumeratedCandidate, ...])
    """

    entity_type = EntityType.MESSAGE

    def __init__(
        self,
        *,
        inbox_root: Path,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "inbox",
        limit: int | None = None,
    ) -> None:
        self._claimer = LocalInboxClaimer(
            inbox_root=inbox_root,
            glob_pattern=_EML_GLOB,
            logical_id_fn=_message_id_from_eml,
            entity_type=EntityType.MESSAGE,
            enumerator_name="eml_local",
            path_metadata_key="eml_path",
            logical_id_metadata_key="message_id",
            mailbox_tenant_id=mailbox_tenant_id,
            principal_mailbox=principal_mailbox,
            container=container,
            limit=limit,
            max_bytes=_MAX_EML_BYTES,
        )

    @property
    def claimed_at_startup_count(self) -> int:
        return self._claimer.claimed_at_startup_count

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        """Enumerate .eml files from inbox/ + claimed/ (crash recovery).

        Returns ``Unsupported`` if the inbox directory is missing or not
        accessible (network drive offline, permissions error). Returns
        ``Success`` with the ordered candidate tuple otherwise.
        """
        return self._claimer.enumerate(intent, correlation_id=correlation_id)

    def mark_processed(self, eml_path: str | Path) -> None:
        """Move a claimed file to ``processed/`` after successful hydration."""
        self._claimer.mark_processed(eml_path)

    def mark_quarantined(self, eml_path: str | Path, *, reason: str) -> None:
        """Move a claimed file to ``quarantine/`` with a companion reason file."""
        self._claimer.mark_quarantined(eml_path, reason=reason)

    def claimed_dir(self) -> Path:
        return self._claimer.claimed_dir()

    def processed_dir(self) -> Path:
        return self._claimer.processed_dir()

    def quarantine_dir(self) -> Path:
        return self._claimer.quarantine_dir()

    def count_quarantine_files(self) -> int:
        """Count ``.eml`` files currently in ``quarantine/`` (for telemetry)."""
        return self._claimer.count_quarantine_files()


__all__ = ["EmlEnumerator"]
