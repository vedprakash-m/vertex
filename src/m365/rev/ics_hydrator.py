"""REV IcsHydrator (Zone C) — FR-PCI-5, Phase 3 (P3-1).

specs/gaps.md P3-1. Hydrates a locally-claimed ``.ics`` (iCalendar) file into a
``HydratedContent`` object for the REV extraction pipeline.

Requires ``icalendar>=5.0``. If the package is absent, ``hydrate`` returns
``Unsupported(icalendar_not_installed)``.

**ICS processing strategy:**

* Only VEVENT components are processed; VALARM, VTODO, VJOURNAL are silently
  skipped (irrelevant to commit/milestone intelligence).
* RECURRENCE-ID components (recurrence exception overrides) are excluded from
  the primary event selection — they are partial overrides whose context is
  embedded in the recurrence rule.
* If the file contains multiple VEVENTs with the same UID (exported recurrence
  exception series), the one with the highest SEQUENCE number is used
  (RFC 5545 §3.6.1 precedence).
* Cancellation: ``STATUS:CANCELLED`` or top-level ``METHOD:CANCEL`` marks the
  event ``metadata_only=True`` with ``route_metadata["cancelled"]=True``.
* Organizer display name is extracted from the ``CN=`` parameter only — the raw
  ``mailto:`` address is never included in canonical text (OA-4 / OA-9 privacy).
* RRULE recurrences are expanded up to ``max_recurrences`` (default 52) using
  ``python-dateutil``; the canonical text includes a compact preview of the
  next few occurrence dates.
* All-day events (DTSTART is a ``datetime.date``): formatted as ``YYYY-MM-DD``.
* Timezone-aware datetimes: converted to UTC for consistent canonical text.
* 10 MB size guard (defensive backstop; IcsEnumerator quarantines at claim time).
* 30s per-file timeout on POSIX; no hard timeout on Windows (local file).

Zone C: imports only ``src.core.*`` + stdlib + icalendar.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from icalendar import Calendar as _ICSCalendar  # type: ignore[import-untyped]
    _ICALENDAR_AVAILABLE = True
except ImportError:
    _ICSCalendar = None  # type: ignore[assignment,misc]
    _ICALENDAR_AVAILABLE = False

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity
from src.core.rev.normalizer import (
    chunk_canonical,
    normalize_whitespace,
    normalized_source_hash,
)
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.result import PortResult, Success, Unsupported

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_MIN_BODY_CHARS = 10
_MAX_ICS_BYTES = 10 * 1024 * 1024
_MAX_RECURRENCES = 52
_RECURRENCE_PREVIEW = 3
_CANCELLED_STATUSES = frozenset({"CANCELLED"})


def _fmt_dt(dt: date | datetime | None) -> str:
    """Format a date or datetime for canonical text."""
    if dt is None:
        return "unknown"
    if isinstance(dt, datetime):
        utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt
        return utc.strftime("%Y-%m-%d %H:%M UTC")
    return dt.strftime("%Y-%m-%d")


def _extract_cn(organizer: Any | None) -> str | None:
    """Extract the CN= display name from an ORGANIZER property.

    Never returns the raw mailto: address — only the CN human-readable name.
    """
    if organizer is None:
        return None
    try:
        params = getattr(organizer, "params", {})
        cn = params.get("CN") if params else None
        return str(cn).strip() or None
    except Exception:
        return None


def _expand_rrule(
    vevent: Any,
    dtstart: date | datetime,
    max_recurrences: int,
) -> list[date | datetime]:
    """Expand a VEVENT's RRULE into up to max_recurrences occurrences.

    Requires ``python-dateutil`` (transitively installed via ``azure-core``).
    Returns an empty list if expansion fails or RRULE is absent.
    """
    rrule_val = getattr(vevent, "get", lambda k, d=None: d)("RRULE")
    if rrule_val is None:
        return []
    try:
        import itertools
        from dateutil.rrule import rrulestr  # type: ignore[import]

        rrule_text = "RRULE:" + rrule_val.to_ical().decode("utf-8")
        if isinstance(dtstart, datetime):
            dt_start: datetime = dtstart.replace(tzinfo=None) if dtstart.tzinfo else dtstart
        else:
            dt_start = datetime(dtstart.year, dtstart.month, dtstart.day)
        rr = rrulestr(rrule_text, dtstart=dt_start, ignoretz=True)
        return list(itertools.islice(rr, max_recurrences))
    except Exception as exc:
        log.debug("IcsHydrator: RRULE expansion failed: %s", exc)
        return []


def _select_primary_vevent(cal: Any) -> Any | None:
    """Select the primary VEVENT: highest SEQUENCE, skipping RECURRENCE-ID exceptions."""
    best: object | None = None
    best_seq: int = -1
    walk = getattr(cal, "walk", None)
    if walk is None:
        return None
    for component in walk():
        if getattr(component, "name", "") != "VEVENT":
            continue
        # Recurrence-ID marks this as an override instance, not the root event
        if component.get("RECURRENCE-ID") is not None:
            continue
        seq = int(component.get("SEQUENCE", 0))
        if best is None or seq > best_seq:
            best = component
            best_seq = seq
    return best


class _TimeoutError(Exception):
    pass


def _alarm_handler(signum: int, frame: object) -> None:
    raise _TimeoutError("IcsHydrator: per-file timeout exceeded")


class IcsHydrator:
    """Hydrate a locally-claimed ``.ics`` file → ``HydratedContent`` (Phase 3, P3-1).

    Satisfies the ``ContentHydrator`` Protocol (Zone A ``src/core/rev/ports.py``).
    ``EnumeratedCandidate.partial_metadata["ics_path"]`` is the path to the
    claimed file; ``partial_metadata["uid"]`` is the canonical dedup key set by
    ``IcsEnumerator``.

    Usage::

        hydrator = IcsHydrator(
            mailbox_tenant_id="tenant-abc",
            principal_mailbox="tpm@example.com",
        )
        result = hydrator.hydrate(candidate, correlation_id="rev-cycle-ics-001")
    """

    def __init__(
        self,
        *,
        mailbox_tenant_id: str,
        principal_mailbox: str,
        container: str = "calendar",
        timeout_seconds: int = _TIMEOUT_SECONDS,
        max_recurrences: int = _MAX_RECURRENCES,
    ) -> None:
        self._tenant_id = mailbox_tenant_id
        self._principal_mailbox = principal_mailbox
        self._container = container
        self._timeout_seconds = timeout_seconds
        self._max_recurrences = max_recurrences

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        """Hydrate one .ics candidate. Returns Unsupported if ics_path is absent."""
        if not _ICALENDAR_AVAILABLE:
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason="icalendar_not_installed: pip install 'icalendar>=5.0'",
            )

        ics_path_str = candidate.partial_metadata.get("ics_path")
        if not ics_path_str:
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason="ics_path_missing: candidate has no ics_path in partial_metadata",
            )
        ics_path = Path(str(ics_path_str))
        if not ics_path.exists():
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason=f"ics_not_found: {ics_path}",
            )

        # Size guard (defensive backstop; IcsEnumerator quarantines at claim time).
        try:
            size = ics_path.stat().st_size
        except OSError:
            size = 0
        if size > _MAX_ICS_BYTES:
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason=f"size_exceeded: {size} bytes > {_MAX_ICS_BYTES}",
            )

        uid = str(candidate.partial_metadata.get("uid", candidate.locator.resource_id))

        # Per-file timeout (POSIX only; SIGALRM not available on Windows).
        use_timeout = sys.platform != "win32" and hasattr(signal, "SIGALRM")
        if use_timeout:
            signal.signal(signal.SIGALRM, _alarm_handler)  # type: ignore[attr-defined]
            signal.alarm(self._timeout_seconds)  # type: ignore[attr-defined]

        try:
            hydrated = self._hydrate_path(ics_path, uid=uid, correlation_id=correlation_id)
        except _TimeoutError:
            log.warning("IcsHydrator: timeout after %ds on %s", self._timeout_seconds, ics_path)
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason=f"hydration_timeout: exceeded {self._timeout_seconds}s",
            )
        except Exception as exc:
            log.warning("IcsHydrator: failed to hydrate %s: %s", ics_path, exc)
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason=f"hydration_error: {exc}",
            )
        finally:
            if use_timeout:
                signal.alarm(0)  # type: ignore[attr-defined]
                signal.signal(signal.SIGALRM, signal.SIG_DFL)  # type: ignore[attr-defined]

        return Success(hydrated)

    def _hydrate_path(
        self,
        ics_path: Path,
        *,
        uid: str,
        correlation_id: str,
    ) -> HydratedContent:
        """Parse a .ics file and build a HydratedContent."""
        data = ics_path.read_bytes()
        cal = _ICSCalendar.from_ical(data)

        # Top-level METHOD:CANCEL (entire calendar is a cancellation notice).
        method = str(cal.get("METHOD", "")).upper().strip()
        is_top_level_cancel = method == "CANCEL"

        vevent = _select_primary_vevent(cal)
        if vevent is None:
            return self._empty_content(uid=uid, correlation_id=correlation_id, note="no_vevent")

        summary = str(vevent.get("SUMMARY", "")).strip()
        description = str(vevent.get("DESCRIPTION", "")).strip()
        location = str(vevent.get("LOCATION", "")).strip()
        status = str(vevent.get("STATUS", "")).upper().strip()
        sequence = int(vevent.get("SEQUENCE", 0))
        organizer = vevent.get("ORGANIZER")
        cn_name = _extract_cn(organizer)

        dtstart = vevent.decoded("DTSTART", None)
        dtend = vevent.decoded("DTEND", None)
        is_allday = isinstance(dtstart, date) and not isinstance(dtstart, datetime)

        is_cancelled = is_top_level_cancel or status in _CANCELLED_STATUSES

        # Recurrence expansion
        has_recurrence = vevent.get("RRULE") is not None
        recurrence_dates: list[date | datetime] = []
        if has_recurrence and dtstart is not None:
            recurrence_dates = _expand_rrule(vevent, dtstart, self._max_recurrences)

        # Build canonical text
        lines: list[str] = []
        if summary:
            lines.append(f"Event: {summary}")
        if cn_name:
            lines.append(f"Organizer: {cn_name}")
        lines.append(f"Date: {_fmt_dt(dtstart)} to {_fmt_dt(dtend)}")
        if is_allday:
            lines.append("(all-day event)")
        if location:
            lines.append(f"Location: {location}")
        if is_cancelled:
            lines.append("Status: CANCELLED")
        if has_recurrence and recurrence_dates:
            preview = [_fmt_dt(d) for d in recurrence_dates[:_RECURRENCE_PREVIEW]]
            lines.append(f"Recurrence: yes (next: {', '.join(preview)})")
        elif has_recurrence:
            lines.append("Recurrence: yes")
        if description:
            lines.append("")
            lines.append(description)

        raw_text = "\n".join(lines)
        canonical = normalize_whitespace(raw_text)

        route_metadata: dict[str, object] = {
            "uid": uid,
            "summary": summary,
            "organizer": cn_name,
            "dtstart": _fmt_dt(dtstart),
            "dtend": _fmt_dt(dtend),
            "location": location or None,
            "cancelled": is_cancelled,
            "is_allday": is_allday,
            "has_recurrence": has_recurrence,
            "sequence": sequence,
        }

        identity = CanonicalItemIdentity(
            source_type=EntityType.EVENT,
            tenant_id=self._tenant_id,
            principal_mailbox=self._principal_mailbox,
            container=self._container,
            resource_id=uid,
        )

        source_hash = normalized_source_hash(canonical if canonical else "")
        chunks = chunk_canonical(canonical) if canonical else ()
        non_ws_len = len(canonical.replace(" ", ""))
        is_metadata_only = is_cancelled or non_ws_len < _MIN_BODY_CHARS

        return HydratedContent(
            identity=identity,
            canonical_text=canonical,
            normalized_source_hash=source_hash,
            chunks=chunks,
            route_metadata=route_metadata,
            hydration_rung="metadata_only" if is_metadata_only else "vevent_body",
            metadata_only=is_metadata_only,
            retrieved_at=datetime.now(timezone.utc),
            correlation_id=correlation_id,
        )

    def _empty_content(
        self,
        *,
        uid: str,
        correlation_id: str,
        note: str,
    ) -> HydratedContent:
        """Return a metadata-only placeholder for unparseable/empty ICS files."""
        identity = CanonicalItemIdentity(
            source_type=EntityType.EVENT,
            tenant_id=self._tenant_id,
            principal_mailbox=self._principal_mailbox,
            container=self._container,
            resource_id=uid,
        )
        canonical = f"ICS event (uid={uid}; note: {note})"
        return HydratedContent(
            identity=identity,
            canonical_text=canonical,
            normalized_source_hash=normalized_source_hash(canonical),
            chunks=(),
            route_metadata={"uid": uid, "parse_note": note},
            hydration_rung="metadata_only",
            metadata_only=True,
            retrieved_at=datetime.now(timezone.utc),
            correlation_id=correlation_id,
        )


__all__ = ["IcsHydrator"]
