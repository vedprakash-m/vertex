"""Shared local-import inbox atomicity (Zone C) — REV multi-surface base.

specs/gaps.md Phase 3 "Adding a surface". The 3-directory atomicity model
(``inbox/`` → ``claimed/`` → ``processed/`` + ``quarantine/``), the FIFO
ordering, the crash-loop guard, the concurrency lock, and the size guard are
identical for every local-import surface (EML, ICS, Teams, docs). This module
factors them into one reusable ``LocalInboxClaimer`` so a new surface is a thin
adapter that supplies a ``glob_pattern`` + a ``logical_id_fn`` (the canonical
dedup key for that surface) — no copy-paste of the atomicity mechanics.

**Crash recovery:** files in ``claimed/`` at startup are prior in-flight items
and are surfaced first (front of the FIFO queue) so the pipeline re-hydrates
them on the next run.

**FIFO ordering:** primary sort key = ``mtime``; secondary = first-8-hex of
``SHA-256(logical_id)`` for deterministic batch-drop tie-breaking.

**Crash-loop guard:** a file found in ``claimed/`` at startup
``_MAX_STARTUP_RECOVERIES`` consecutive times is presumed poisonous →
quarantined with ``reason=crash_loop`` so a poison file cannot loop forever.

**Concurrency guard:** a ``portalocker`` ``cycle.lock`` in ``claimed/``
prevents two concurrent invocations from claiming the same file.

**Network-drive OSError fallback:** cross-filesystem ``os.rename`` (``EXDEV``)
falls back to ``shutil.copy2 + fsync + unlink`` so the copy is durable before
the original is removed.

Zone C: imports only ``src.core.*`` + stdlib + portalocker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import portalocker

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, PortResult, Success, Unsupported

log = logging.getLogger(__name__)

LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_LIMIT = 100
DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file (oversized → quarantine)
# Crash-loop guard: a file found in claimed/ at startup this many consecutive
# times is presumed to crash every cycle → quarantine with reason=crash_loop.
DEFAULT_MAX_STARTUP_RECOVERIES = 3
_CRASH_LOOP_STORE = "_crash_loop_counts.json"   # co-located with the inbox


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def fifo_key(path: Path, logical_id: str) -> tuple[float, str]:
    """Sort key: (mtime, first-8-hex of SHA-256(logical_id)) for FIFO + tie-break."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, sha256_hex(logical_id)[:8])


def atomic_rename(src: Path, dst: Path) -> None:
    """Rename src → dst; falls back to copy+unlink if cross-filesystem."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as exc:
        # EXDEV (cross-device link) or similar — copy then unlink.
        tmp = dst.with_suffix(".tmp~")
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
        try:
            src.unlink()
        except OSError:
            log.warning("atomic_rename: could not unlink source %s after copy: %s", src, exc)


class LocalInboxClaimer:
    """Reusable 3-dir atomicity claimer for any local-import surface.

    A surface supplies ``glob_pattern`` (e.g. ``*.ics``) and ``logical_id_fn``
    (``Path → str``: the canonical dedup key — Message-ID for EML, UID for ICS).
    The claimer handles directory creation, the lock, FIFO ordering, the
    crash-loop guard, the size guard, atomic claim, and ``mark_processed`` /
    ``mark_quarantined``.

    ``path_metadata_key`` is the partial_metadata key under which the claimed
    file path is stashed for the surface's hydrator (``eml_path`` for EML,
    ``ics_path`` for ICS, …). ``enumerator_name`` is the telemetry tag
    (``eml_local`` / ``ics_local`` / …).
    """

    def __init__(
        self,
        *,
        inbox_root: Path,
        glob_pattern: str,
        logical_id_fn: Callable[[Path], str],
        entity_type: EntityType,
        enumerator_name: str,
        path_metadata_key: str,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "inbox",
        limit: int | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_startup_recoveries: int = DEFAULT_MAX_STARTUP_RECOVERIES,
        logical_id_metadata_key: str = "logical_id",
    ) -> None:
        self._inbox = inbox_root
        self._claimed = inbox_root / "claimed"
        self._processed = inbox_root / "processed"
        self._quarantine = inbox_root / "quarantine"
        self._lock_path = self._claimed / "cycle.lock"
        self._glob = glob_pattern
        self._logical_id_fn = logical_id_fn
        self._entity_type = entity_type
        self._enumerator_name = enumerator_name
        self._path_key = path_metadata_key
        self._id_key = logical_id_metadata_key
        self._tenant_id = mailbox_tenant_id
        self._principal_mailbox = principal_mailbox
        self._container = container
        self._limit = limit
        self._max_bytes = max_bytes
        self._max_recoveries = max_startup_recoveries
        self.claimed_at_startup_count: int = 0

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        """Enumerate files from inbox/ + claimed/ (crash recovery)."""
        try:
            return self._enumerate_safe(intent, correlation_id=correlation_id)
        except OSError as exc:
            log.warning("%s: OSError accessing %s — %s", self._enumerator_name, self._inbox, exc)
            return Unsupported(
                entity_type=self._entity_type.value,
                reason=f"inbox_oserror: {exc}",
            )

    def _enumerate_safe(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._claimed.mkdir(parents=True, exist_ok=True)
        self._processed.mkdir(parents=True, exist_ok=True)
        self._quarantine.mkdir(parents=True, exist_ok=True)

        limit = self._limit or getattr(intent, "limit", None) or DEFAULT_LIMIT

        try:
            lock_fh = open(self._lock_path, "w", encoding="utf-8")
            portalocker.lock(lock_fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, OSError) as exc:
            log.warning("%s: could not acquire cycle.lock (%s) — skipping", self._enumerator_name, exc)
            return Unsupported(
                entity_type=self._entity_type.value,
                reason=f"cycle_lock_contention: {exc}",
            )

        try:
            return self._claim_and_build(limit, correlation_id=correlation_id)
        finally:
            try:
                portalocker.unlock(lock_fh)
                lock_fh.close()
            except Exception:
                pass

    def _claim_and_build(
        self,
        limit: int,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        claimed_at_startup = sorted(self._claimed.glob(self._glob))
        self.claimed_at_startup_count = len(claimed_at_startup)

        recovery_keyed, _quarantined = self._apply_crash_loop_guard(claimed_at_startup)
        claimed_ids = {p: self._logical_id_fn(p) for p in recovery_keyed}
        recovery_keyed = sorted(recovery_keyed, key=lambda p: fifo_key(p, claimed_ids[p]))

        inbox_files = sorted(self._inbox.glob(self._glob))
        inbox_ids = {p: self._logical_id_fn(p) for p in inbox_files}
        inbox_keyed = sorted(inbox_files, key=lambda p: fifo_key(p, inbox_ids[p]))

        remaining_slots = max(0, limit - len(recovery_keyed))
        newly_claimed: list[Path] = []
        for inbox_path in inbox_keyed[:remaining_slots]:
            try:
                size = inbox_path.stat().st_size
            except OSError:
                size = 0
            if size > self._max_bytes:
                log.warning("%s: quarantining oversized %s (%d bytes)",
                            self._enumerator_name, inbox_path, size)
                self.mark_quarantined(inbox_path, reason=f"size_exceeded: {size} bytes")
                continue
            claimed_path = self._claimed / inbox_path.name
            try:
                atomic_rename(inbox_path, claimed_path)
                newly_claimed.append(claimed_path)
            except OSError as exc:
                log.warning("%s: failed to claim %s → %s: %s",
                            self._enumerator_name, inbox_path, claimed_path, exc)

        truncated = len(inbox_keyed) > remaining_slots or len(recovery_keyed) > limit

        all_claimed = recovery_keyed + newly_claimed
        all_ids = {**claimed_ids}
        for p in newly_claimed:
            all_ids[p] = inbox_ids.get(self._inbox / p.name, self._logical_id_fn(p))

        candidates: list[EnumeratedCandidate] = []
        now = datetime.now(timezone.utc)
        for path in all_claimed[:limit]:
            lid = all_ids.get(path, self._logical_id_fn(path))
            is_recovery = path in claimed_at_startup
            relevance = 1.0 if is_recovery else 0.9
            locator = HydrationLocator(
                source_type=self._entity_type,
                tenant_id=self._tenant_id,
                principal_mailbox=self._principal_mailbox,
                container=self._container,
                resource_id=lid,
            )
            meta = {
                self._path_key: str(path),
                self._id_key: lid,
                "is_recovery": is_recovery,
                "claimed_at": now.isoformat(),
            }
            candidates.append(EnumeratedCandidate(
                locator=locator,
                relevance_score=relevance,
                partial_metadata=meta,
                correlation_id=correlation_id,
                enumerator=self._enumerator_name,
                received_at=now,
            ))

        result_tuple = tuple(candidates)
        if truncated:
            return Incomplete(
                value=result_tuple,
                reason=(
                    f"budget_stop: inbox has {len(inbox_keyed)} files, "
                    f"recovery has {len(recovery_keyed)}, limit={limit}"
                ),
            )
        return Success(result_tuple)

    # ------------------------------------------------------------------
    # Crash-loop guard
    # ------------------------------------------------------------------

    def _crash_loop_path(self) -> Path:
        return self._inbox / _CRASH_LOOP_STORE

    def _load_crash_loop_counts(self) -> dict[str, int]:
        path = self._crash_loop_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            log.warning("%s: crash-loop store unreadable (%s) — resetting",
                        self._enumerator_name, exc)
            return {}

    def _save_crash_loop_counts(self, counts: dict[str, int]) -> None:
        path = self._crash_loop_path()
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("%s: could not persist crash-loop counts: %s", self._enumerator_name, exc)

    def _reset_crash_loop_count(self, name: str) -> None:
        counts = self._load_crash_loop_counts()
        if name in counts:
            counts.pop(name, None)
            self._save_crash_loop_counts(counts)

    def _apply_crash_loop_guard(
        self, claimed_at_startup: list[Path],
    ) -> tuple[list[Path], list[Path]]:
        if not claimed_at_startup:
            counts = self._load_crash_loop_counts()
            if counts:
                self._save_crash_loop_counts({})
            return [], []
        counts = self._load_crash_loop_counts()
        survivors: list[Path] = []
        quarantined: list[Path] = []
        for p in claimed_at_startup:
            name = p.name
            counts[name] = counts.get(name, 0) + 1
            if counts[name] >= self._max_recoveries:
                log.warning("%s: crash-loop — quarantining %s after %d consecutive startup recoveries",
                            self._enumerator_name, name, counts[name])
                self.mark_quarantined(p, reason=f"crash_loop: {counts[name]} consecutive startup recoveries")
                counts.pop(name, None)
                quarantined.append(p)
            else:
                survivors.append(p)
        self._save_crash_loop_counts(counts)
        return survivors, quarantined

    # ------------------------------------------------------------------
    # Disposition
    # ------------------------------------------------------------------

    def mark_processed(self, path: str | Path) -> None:
        src = Path(path)
        if not src.exists():
            return
        dst = self._processed / src.name
        try:
            atomic_rename(src, dst)
        except OSError as exc:
            log.warning("%s: could not mark %s as processed: %s", self._enumerator_name, src, exc)
            return
        self._reset_crash_loop_count(src.name)

    def mark_quarantined(self, path: str | Path, *, reason: str) -> None:
        src = Path(path)
        if not src.exists():
            return
        dst = self._quarantine / src.name
        try:
            atomic_rename(src, dst)
            reason_path = dst.with_suffix(".reason.txt")
            reason_path.write_text(reason, encoding="utf-8")
        except OSError as exc:
            log.warning("%s: could not quarantine %s: %s", self._enumerator_name, src, exc)
            return
        if not reason.startswith("crash_loop"):
            self._reset_crash_loop_count(src.name)

    def claimed_dir(self) -> Path:
        return self._claimed

    def processed_dir(self) -> Path:
        return self._processed

    def quarantine_dir(self) -> Path:
        return self._quarantine

    def count_quarantine_files(self) -> int:
        if not self._quarantine.exists():
            return 0
        return sum(1 for _ in self._quarantine.glob(self._glob))


__all__ = [
    "LocalInboxClaimer",
    "atomic_rename",
    "fifo_key",
    "sha256_hex",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_STARTUP_RECOVERIES",
]