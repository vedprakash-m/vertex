from __future__ import annotations

from src.m365.graph_calendar_client import CalendarEventPage, CalendarEventRecord
from src.m365.workiq_calendar_discovery import WorkIQCalendarDiscovery


class _FakeCalendarClient:
    def __init__(self, pages: dict[str, CalendarEventPage]) -> None:
        self.pages = pages
        self.queries: list[str] = []

    def search_events(
        self,
        *,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> CalendarEventPage:
        self.allow_cli_fallback = allow_cli_fallback
        self.queries.append(query)
        return self.pages.get(query, CalendarEventPage(records=(), next_cursor=None, source="workiq"))


class _FakeTranscriptReader:
    def __init__(self, titles: dict[str, str | None]) -> None:
        self.titles = titles
        self.calls: list[str] = []

    def get_transcript(self, *, meeting_id: str):
        self.calls.append(meeting_id)
        title = self.titles.get(meeting_id)
        if title is None:
            return None

        class _Record:
            def __init__(self, title: str) -> None:
                self.title = title

        return _Record(title)


def test_workiq_calendar_discovery_prefers_owner_matched_recurring_candidate() -> None:
    calendar_client = _FakeCalendarClient(
        {
            "Firmware Review Sync": CalendarEventPage(
                records=(
                    CalendarEventRecord(
                        source_id="event-1",
                        subject="Firmware Review",
                        organizer="operator@example.com",
                        start_at=None,
                        end_at=None,
                        location=None,
                        web_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-a",
                        series_master_id="series-a",
                        is_recurring=True,
                    ),
                    CalendarEventRecord(
                        source_id="event-2",
                        subject="Firmware Review",
                        organizer="other@example.com",
                        start_at=None,
                        end_at=None,
                        location=None,
                        web_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-b",
                        series_master_id="series-b",
                    ),
                ),
                next_cursor=None,
                source="workiq",
            )
        }
    )
    discovery = WorkIQCalendarDiscovery(
        calendar_client=calendar_client,
        transcript_reader=_FakeTranscriptReader({}),
    )

    candidates = discovery.discover_candidates(
        "Firmware Review Sync",
        limit=5,
        owner_aliases=("operator@example.com",),
    )

    assert [candidate.discovered_id for candidate in candidates] == ["series-a", "series-b"]
    assert candidates[0].match_score > candidates[1].match_score


def test_workiq_calendar_discovery_caps_transcript_probes_and_uses_transcript_title() -> None:
    calendar_client = _FakeCalendarClient(
        {
            "Acme Weekly Ops Review": CalendarEventPage(
                records=(
                    CalendarEventRecord(
                        source_id="event-1",
                        subject="Weekly Ops Review",
                        organizer=None,
                        start_at=None,
                        end_at=None,
                        location=None,
                        web_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-1",
                        meeting_id="meeting-1",
                        series_master_id="series-1",
                    ),
                    CalendarEventRecord(
                        source_id="event-2",
                        subject="Weekly Ops Sync",
                        organizer=None,
                        start_at=None,
                        end_at=None,
                        location=None,
                        web_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-2",
                        meeting_id="meeting-2",
                        series_master_id="series-2",
                    ),
                    CalendarEventRecord(
                        source_id="event-3",
                        subject="Weekly Ops Notes",
                        organizer=None,
                        start_at=None,
                        end_at=None,
                        location=None,
                        web_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-3",
                        meeting_id="meeting-3",
                        series_master_id="series-3",
                    ),
                ),
                next_cursor=None,
                source="workiq",
            )
        }
    )
    transcript_reader = _FakeTranscriptReader(
        {
            "meeting-1": "Acme Weekly Ops Review",
            "meeting-2": "Acme Weekly Ops Review",
            "meeting-3": "Acme Weekly Ops Review",
        }
    )
    discovery = WorkIQCalendarDiscovery(
        calendar_client=calendar_client,
        transcript_reader=transcript_reader,
    )

    candidates = discovery.discover_candidates("Acme Weekly Ops Review", limit=5)

    assert len(transcript_reader.calls) == 2
    assert candidates[0].discovered_id == "series-1"
    assert candidates[0].exact_match is True


def test_p4_22_discovery_allows_cli_fallback_and_300s_timeout() -> None:
    """P4-22: discovery must allow the CLI fallback and use a 300s query timeout.

    workiq.exe exits with code 1 after every request (unconditional), so callers
    with allow_cli_fallback=False silently return zero candidates. The CLI path is
    the reliable one. A 300s timeout covers the empirically observed 40-250s tail.
    """
    from src.m365 import workiq_calendar_discovery as cal_mod
    from src.m365 import workiq_mail_discovery as mail_mod
    from src.m365 import registry_id_discovery as reg_mod

    # Module-level constants raised 45 → 300 (query) and 120 → 900 (artifact budget,
    # so all 3 attempts can run at the 300s tail — ~300s x 3 per artifact).
    assert cal_mod._DISCOVERY_QUERY_TIMEOUT_SECONDS == 300
    assert cal_mod._DISCOVERY_ARTIFACT_BUDGET_SECONDS == 900
    assert mail_mod._DISCOVERY_QUERY_TIMEOUT_SECONDS == 300
    assert mail_mod._DISCOVERY_ARTIFACT_BUDGET_SECONDS == 900
    assert reg_mod._DISCOVERY_QUERY_TIMEOUT_SECONDS == 300
    assert reg_mod._DISCOVERY_ARTIFACT_BUDGET_SECONDS == 900

    calendar_client = _FakeCalendarClient(
        {"Acme Weekly Ops Review": CalendarEventPage(records=(), next_cursor=None, source="workiq")}
    )
    discovery = WorkIQCalendarDiscovery(
        calendar_client=calendar_client,
        transcript_reader=_FakeTranscriptReader({}),
    )
    discovery.discover_candidates("Acme Weekly Ops Review", limit=5)
    # The discovery caller must propagate allow_cli_fallback=True to the search facade.
    assert calendar_client.allow_cli_fallback is True
