"""Unit tests for IcsEnumerator (P3-1) and IcsHydrator (P3-1).

Covers:
* IcsEnumerator: 3-dir atomicity, FIFO ordering, crash recovery, limit,
  uid extraction, sha256-fallback ID, quarantine
* IcsHydrator: basic event, all-day, cancelled, recurrence, organizer CN,
  missing ics_path, icalendar-not-installed guard, no-vevent fallback
* End-to-end: IcsEnumerator → IcsHydrator → HydratedContent
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, Success, Unsupported
from src.m365.rev.ics_enumerator import IcsEnumerator, uid_from_ics
from src.m365.rev.ics_hydrator import IcsHydrator

icalendar = pytest.importorskip("icalendar", reason="icalendar>=5.0 not installed")

# ---------------------------------------------------------------------------
# ICS fixtures (inline text)
# ---------------------------------------------------------------------------

_BASIC_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:event-gen9-deployment@example.com
SUMMARY:Gen9 BIOS AP Deployment Complete
DTSTART:20260624T140000Z
DTEND:20260624T150000Z
ORGANIZER;CN=Firmware Lead:mailto:fwlead@example.com
DESCRIPTION:All Gen9 devices migrated to new firmware. Rollout complete.
STATUS:CONFIRMED
SEQUENCE:0
END:VEVENT
END:VCALENDAR
"""

_ALLDAY_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:allday-milestone-001@example.com
SUMMARY:Q2 Milestone Freeze
DTSTART;VALUE=DATE:20260630
DTEND;VALUE=DATE:20260701
ORGANIZER;CN=PM Lead:mailto:pm@example.com
DESCRIPTION:Q2 feature freeze milestone.
END:VEVENT
END:VCALENDAR
"""

_CANCELLED_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
METHOD:CANCEL
BEGIN:VEVENT
UID:cancelled-event-001@example.com
SUMMARY:Cancelled Review Meeting
DTSTART:20260624T160000Z
DTEND:20260624T170000Z
STATUS:CANCELLED
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

_RECURRING_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:weekly-sync-001@example.com
SUMMARY:Weekly NOVA Sync
DTSTART:20260624T150000Z
DTEND:20260624T160000Z
ORGANIZER;CN=TPM Lead:mailto:tpm@example.com
RRULE:FREQ=WEEKLY;COUNT=10
DESCRIPTION:Weekly alignment sync for NOVA workstream.
END:VEVENT
END:VCALENDAR
"""

_SEQUENCE_HIGH_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:seq-test-001@example.com
SUMMARY:Original Event
DTSTART:20260624T140000Z
SEQUENCE:0
END:VEVENT
BEGIN:VEVENT
UID:seq-test-001@example.com
SUMMARY:Updated Event Title
DTSTART:20260624T150000Z
SEQUENCE:2
END:VEVENT
END:VCALENDAR
"""

_NO_UID_VEVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
SUMMARY:Event Without UID
DTSTART:20260624T140000Z
END:VEVENT
END:VCALENDAR
"""

_EMPTY_CALENDAR = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
END:VCALENDAR
"""

_VALARM_ONLY = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VTODO
UID:todo-001@example.com
SUMMARY:A to-do item (should be skipped)
END:VTODO
END:VCALENDAR
"""


def _write_ics(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).strip(), encoding="utf-8")
    return path


def _make_intent(limit: int = 10) -> RetrievalIntent:
    return RetrievalIntent(entity_type=EntityType.EVENT, limit=limit)


def _make_candidate(ics_path: Path, uid: str) -> EnumeratedCandidate:
    return EnumeratedCandidate(
        locator=HydrationLocator(
            source_type=EntityType.EVENT,
            tenant_id="tenant-test",
            principal_mailbox="tpm@example.com",
            container="calendar",
            resource_id=uid,
        ),
        relevance_score=0.9,
        partial_metadata={
            "ics_path": str(ics_path),
            "uid": uid,
            "is_recovery": False,
            "claimed_at": "2026-06-24T10:00:00+00:00",
        },
        correlation_id="test-cid",
        enumerator="ics_local",
    )


# ---------------------------------------------------------------------------
# uid_from_ics helper
# ---------------------------------------------------------------------------


class TestUidFromIcs:
    def test_extracts_uid_from_valid_ics(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "e.ics", _BASIC_VEVENT)
        uid = uid_from_ics(p)
        assert uid == "event-gen9-deployment@example.com"

    def test_fallback_sha256_when_no_uid(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "nouid.ics", _NO_UID_VEVENT)
        uid = uid_from_ics(p)
        assert uid.startswith("sha256:")

    def test_sha256_stable_for_same_content(self, tmp_path: Path) -> None:
        p1 = _write_ics(tmp_path / "a.ics", _NO_UID_VEVENT)
        p2 = _write_ics(tmp_path / "b.ics", _NO_UID_VEVENT)
        assert uid_from_ics(p1) == uid_from_ics(p2)

    def test_returns_sha256_for_nonexistent_file(self, tmp_path: Path) -> None:
        uid = uid_from_ics(tmp_path / "ghost.ics")
        assert uid.startswith("sha256:")


# ---------------------------------------------------------------------------
# IcsEnumerator tests
# ---------------------------------------------------------------------------


class TestIcsEnumerator:
    def _enumerator(self, inbox: Path) -> IcsEnumerator:
        return IcsEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="tenant-test",
            principal_mailbox="tpm@example.com",
        )

    def test_empty_inbox_returns_empty_success(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c1")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_single_ics_enumerated_and_claimed(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "event001.ics", _BASIC_VEVENT)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c2")
        assert isinstance(result, Success)
        assert len(result.value) == 1
        candidate = result.value[0]
        assert candidate.enumerator == "ics_local"
        assert candidate.locator.resource_id == "event-gen9-deployment@example.com"
        # File should be in claimed/ now.
        assert len(list(enum.claimed_dir().glob("*.ics"))) == 1
        assert not (inbox / "event001.ics").exists()

    def test_multiple_files_fifo_ordering(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        import os
        for i, (name, content) in enumerate([
            ("a.ics", _BASIC_VEVENT),
            ("b.ics", _ALLDAY_VEVENT),
            ("c.ics", _RECURRING_VEVENT),
        ]):
            p = inbox / name
            _write_ics(p, content)
            os.utime(p, (1000 + i, 1000 + i))

        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c3")
        assert isinstance(result, Success)
        assert len(result.value) == 3
        paths = [c.partial_metadata["ics_path"] for c in result.value]
        assert "a.ics" in paths[0]
        assert "b.ics" in paths[1]
        assert "c.ics" in paths[2]

    def test_crash_recovery_claimed_files_surface_first(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        enum = self._enumerator(inbox)
        # Put a file directly in claimed/ (simulates prior crash).
        claimed = enum.claimed_dir()
        claimed.mkdir(parents=True, exist_ok=True)
        _write_ics(claimed / "crash.ics", _BASIC_VEVENT)
        # And put a new file in inbox/.
        _write_ics(inbox / "new.ics", _ALLDAY_VEVENT)
        result = enum.enumerate(_make_intent(), correlation_id="c4")
        assert isinstance(result, Success)
        assert len(result.value) == 2
        assert "crash.ics" in result.value[0].partial_metadata["ics_path"]
        assert result.value[0].relevance_score == 1.0  # recovery gets 1.0
        assert result.value[1].relevance_score == 0.9

    def test_limit_respected_returns_incomplete(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        for i in range(5):
            _write_ics(
                inbox / f"event{i:03d}.ics",
                _BASIC_VEVENT.replace(
                    "event-gen9-deployment@example.com",
                    f"event-{i:03d}@example.com",
                ),
            )
        enum = IcsEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
            limit=3,
        )
        result = enum.enumerate(_make_intent(), correlation_id="c5")
        assert isinstance(result, Incomplete)
        assert len(result.value) == 3

    def test_mark_processed_moves_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "done.ics", _BASIC_VEVENT)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c6")
        assert isinstance(result, Success)
        ics_path = result.value[0].partial_metadata["ics_path"]
        enum.mark_processed(ics_path)
        assert not Path(ics_path).exists()
        assert (enum.processed_dir() / "done.ics").exists()

    def test_mark_quarantined_creates_reason_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "bad.ics", _EMPTY_CALENDAR)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c7")
        assert isinstance(result, Success)
        ics_path = result.value[0].partial_metadata["ics_path"]
        enum.mark_quarantined(ics_path, reason="no_vevent")
        assert not Path(ics_path).exists()
        q_dir = enum.quarantine_dir()
        assert (q_dir / "bad.ics").exists()
        assert "no_vevent" in (q_dir / "bad.reason.txt").read_text()

    def test_sha256_fallback_id_for_no_uid_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "nouid.ics", _NO_UID_VEVENT)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c8")
        assert isinstance(result, Success)
        rid = result.value[0].locator.resource_id
        assert rid.startswith("sha256:")


# ---------------------------------------------------------------------------
# IcsHydrator tests
# ---------------------------------------------------------------------------


class TestIcsHydrator:
    def _hydrator(self) -> IcsHydrator:
        return IcsHydrator(
            mailbox_tenant_id="tenant-test",
            principal_mailbox="tpm@example.com",
        )

    def _candidate(self, ics_path: Path, uid: str) -> EnumeratedCandidate:
        return _make_candidate(ics_path, uid)

    def test_basic_vevent_hydrated(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "event.ics", _BASIC_VEVENT)
        hydrator = self._hydrator()
        uid = "event-gen9-deployment@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h1")
        assert isinstance(result, Success)
        content = result.value
        assert isinstance(content, HydratedContent)
        assert "Gen9 BIOS AP Deployment Complete" in content.canonical_text
        assert content.metadata_only is False
        assert content.chunks

    def test_organizer_cn_extracted_not_smtp(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "event.ics", _BASIC_VEVENT)
        hydrator = self._hydrator()
        uid = "event-gen9-deployment@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h2")
        assert isinstance(result, Success)
        text = result.value.canonical_text
        # CN= display name should appear
        assert "Firmware Lead" in text
        # Raw mailto: address must NOT appear in canonical text
        assert "fwlead@example.com" not in text

    def test_allday_event_formatted_as_date(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "allday.ics", _ALLDAY_VEVENT)
        hydrator = self._hydrator()
        uid = "allday-milestone-001@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h3")
        assert isinstance(result, Success)
        content = result.value
        assert "2026-06-30" in content.canonical_text
        assert "(all-day event)" in content.canonical_text
        assert content.route_metadata.get("is_allday") is True

    def test_cancelled_event_is_metadata_only(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "cancel.ics", _CANCELLED_VEVENT)
        hydrator = self._hydrator()
        uid = "cancelled-event-001@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h4")
        assert isinstance(result, Success)
        content = result.value
        assert content.metadata_only is True
        assert content.route_metadata.get("cancelled") is True
        assert "CANCELLED" in content.canonical_text

    def test_recurring_event_expands_rrule(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "recur.ics", _RECURRING_VEVENT)
        hydrator = self._hydrator()
        uid = "weekly-sync-001@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h5")
        assert isinstance(result, Success)
        content = result.value
        assert content.route_metadata.get("has_recurrence") is True
        assert "Recurrence: yes" in content.canonical_text

    def test_sequence_highest_wins(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "seq.ics", _SEQUENCE_HIGH_VEVENT)
        hydrator = self._hydrator()
        uid = "seq-test-001@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="h6")
        assert isinstance(result, Success)
        content = result.value
        # SEQUENCE=2 has SUMMARY "Updated Event Title"
        assert "Updated Event Title" in content.canonical_text
        assert "Original Event" not in content.canonical_text

    def test_missing_ics_path_returns_unsupported(self) -> None:
        hydrator = self._hydrator()
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.EVENT,
                tenant_id="t",
                principal_mailbox="u@x.com",
                container="calendar",
                resource_id="no-path",
            ),
            relevance_score=0.9,
            partial_metadata={},  # no ics_path
            correlation_id="hx",
            enumerator="ics_local",
        )
        result = hydrator.hydrate(candidate, correlation_id="hx")
        assert isinstance(result, Unsupported)
        assert "ics_path_missing" in result.reason

    def test_nonexistent_path_returns_unsupported(self, tmp_path: Path) -> None:
        hydrator = self._hydrator()
        result = hydrator.hydrate(
            _make_candidate(tmp_path / "ghost.ics", "ghost-uid"),
            correlation_id="hy",
        )
        assert isinstance(result, Unsupported)
        assert "ics_not_found" in result.reason

    def test_empty_calendar_returns_metadata_only(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "empty.ics", _EMPTY_CALENDAR)
        hydrator = self._hydrator()
        result = hydrator.hydrate(_make_candidate(p, "empty-uid"), correlation_id="hz")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_vtodo_only_returns_metadata_only(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "todo.ics", _VALARM_ONLY)
        hydrator = self._hydrator()
        result = hydrator.hydrate(_make_candidate(p, "todo-uid"), correlation_id="ht")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_route_metadata_populated(self, tmp_path: Path) -> None:
        p = _write_ics(tmp_path / "event.ics", _BASIC_VEVENT)
        hydrator = self._hydrator()
        uid = "event-gen9-deployment@example.com"
        result = hydrator.hydrate(self._candidate(p, uid), correlation_id="hm")
        assert isinstance(result, Success)
        meta = result.value.route_metadata
        assert meta.get("uid") == uid
        assert "2026-06-24" in str(meta.get("dtstart", ""))
        assert meta.get("cancelled") is False
        assert meta.get("has_recurrence") is False

    def test_size_guard_returns_unsupported(self, tmp_path: Path) -> None:
        import src.m365.rev.ics_hydrator as _mod
        orig = _mod._MAX_ICS_BYTES
        _mod._MAX_ICS_BYTES = 10  # 10 bytes limit
        try:
            p = _write_ics(tmp_path / "big.ics", _BASIC_VEVENT)
            hydrator = self._hydrator()
            result = hydrator.hydrate(_make_candidate(p, "uid-big"), correlation_id="hs")
            assert isinstance(result, Unsupported)
            assert "size_exceeded" in result.reason
        finally:
            _mod._MAX_ICS_BYTES = orig


# ---------------------------------------------------------------------------
# End-to-end: IcsEnumerator → IcsHydrator
# ---------------------------------------------------------------------------


class TestIcsEnumeratorHydratorEndToEnd:
    def test_enumerate_then_hydrate(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "deploy.ics", _BASIC_VEVENT)

        enumerator = IcsEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
        )
        hydrator = IcsHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
        )

        enum_result = enumerator.enumerate(
            RetrievalIntent(entity_type=EntityType.EVENT, limit=10),
            correlation_id="e2e-1",
        )
        assert isinstance(enum_result, Success)
        assert len(enum_result.value) == 1

        candidate = enum_result.value[0]
        hydrate_result = hydrator.hydrate(candidate, correlation_id="e2e-1")
        assert isinstance(hydrate_result, Success)
        hydrated = hydrate_result.value
        assert "Gen9" in hydrated.canonical_text
        assert not hydrated.metadata_only
        assert hydrated.identity.resource_id == "event-gen9-deployment@example.com"

    def test_enumerate_and_mark_processed(self, tmp_path: Path) -> None:
        inbox = tmp_path / "cal_inbox"
        inbox.mkdir()
        _write_ics(inbox / "event.ics", _BASIC_VEVENT)

        enumerator = IcsEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
        )
        result = enumerator.enumerate(_make_intent(), correlation_id="e2e-2")
        assert isinstance(result, Success)
        path = result.value[0].partial_metadata["ics_path"]
        enumerator.mark_processed(path)
        assert (enumerator.processed_dir() / "event.ics").exists()
