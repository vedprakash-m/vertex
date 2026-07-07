"""REV IcsEnumerator (Zone C) — FR-PCI-2, Phase 3 (P3-1).

specs/gaps.md P3-1. Enumerates locally-exported ``.ics`` (iCalendar) files from
an ``inbox/`` directory using the shared 3-directory atomicity model
(``LocalInboxClaimer``). The canonical dedup key for a calendar item is its
``UID`` property (RFC 5545) — extracted with a lightweight stdlib scan so the
enumerator does **not** require the optional ``icalendar`` extra (only the
hydrator does). If a file has no ``UID``, the logical id is a stable
``sha256:<hash>`` over the file bytes so re-imports of the same file collapse.

FIFO ordering, crash recovery, crash-loop guard, concurrency lock, size guard,
and ``processed/`` / ``quarantine/`` disposition are inherited from
``LocalInboxClaimer`` (identical to the EML surface — one atomicity impl, many
surfaces).

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import PortResult
from src.m365.rev.local_inbox import LocalInboxClaimer, sha256_hex

log = logging.getLogger(__name__)

_ICS_GLOB = "*.ics"
_MAX_ICS_BYTES = 10 * 1024 * 1024   # 10 MB per .ics (oversized → quarantine)
# RFC 5545: a property line is ``NAME;PARAMS:VALUE`` (``:`` separates the value).
# UID has no params in practice; match the first UID property at line start.
_UID_RE = re.compile(r"(?im)^UID\s*(?:;[^:]*)?\s*:(.+?)\s*$")


def uid_from_ics(path: Path) -> str:
    """Return the first VEVENT ``UID`` value, or a stable content hash fallback.

    Stdlib-only (no ``icalendar`` dependency) so the enumerator works on a bare
    install. The hydrator performs the full structured parse.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"sha256:{sha256_hex(path.stem)[:32]}"
    # Restrict to the first VEVENT block so a calendar's VALARM/VTODO UIDs are
    # never mistaken for the event UID.
    vevent_start = text.find("BEGIN:VEVENT")
    block = text[vevent_start:] if vevent_start >= 0 else text
    end = block.find("END:VEVENT")
    if end >= 0:
        block = block[:end]
    m = _UID_RE.search(block)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # No UID → stable hash over the file content (re-imports collapse).
    return f"sha256:{sha256_hex(text)[:32]}"


class IcsEnumerator:
    """Enumerate locally-exported ``.ics`` files for the REV pipeline (Phase 3).

    Satisfies the ``CandidateEnumerator`` Protocol. The ``RetrievalIntent`` is
    used for ``tenant_id`` / ``principal_mailbox`` / ``container`` metadata only;
    the ICS content (``UID``) determines the dedup key. Inbox root convention:
    ``programs/{program_id}/rev_calendar_inbox/``.

    Usage::

        enumerator = IcsEnumerator(
            inbox_root=Path("programs/{program_id}/rev_calendar_inbox"),
            mailbox_tenant_id="tenant-abc",
            principal_mailbox="tpm@example.com",
        )
        result = enumerator.enumerate(intent, correlation_id="rev-cycle-ics-001")
    """

    entity_type = EntityType.EVENT

    def __init__(
        self,
        *,
        inbox_root: Path,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "calendar",
        limit: int | None = None,
    ) -> None:
        self._claimer = LocalInboxClaimer(
            inbox_root=inbox_root,
            glob_pattern=_ICS_GLOB,
            logical_id_fn=uid_from_ics,
            entity_type=EntityType.EVENT,
            enumerator_name="ics_local",
            path_metadata_key="ics_path",
            logical_id_metadata_key="uid",
            mailbox_tenant_id=mailbox_tenant_id,
            principal_mailbox=principal_mailbox,
            container=container,
            limit=limit,
            max_bytes=_MAX_ICS_BYTES,
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
        """Enumerate .ics files from inbox/ + claimed/ (crash recovery)."""
        return self._claimer.enumerate(intent, correlation_id=correlation_id)

    def mark_processed(self, ics_path: str | Path) -> None:
        self._claimer.mark_processed(ics_path)

    def mark_quarantined(self, ics_path: str | Path, *, reason: str) -> None:
        self._claimer.mark_quarantined(ics_path, reason=reason)

    def claimed_dir(self) -> Path:
        return self._claimer.claimed_dir()

    def processed_dir(self) -> Path:
        return self._claimer.processed_dir()

    def quarantine_dir(self) -> Path:
        return self._claimer.quarantine_dir()

    def count_quarantine_files(self) -> int:
        return self._claimer.count_quarantine_files()


__all__ = ["IcsEnumerator", "uid_from_ics"]