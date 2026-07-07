from __future__ import annotations

from src.core.exceptions import QueryError
from src.m365.transcript_reader import TranscriptReader


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.responses: list[dict[str, object] | Exception | None] = []
        self.workiq_questions: list[str] = []
        self.workiq_responses: list[dict[str, object] | Exception | None] = []

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object]):
        self.calls.append((server, tool, args))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def ask_workiq(self, question: str):
        self.workiq_questions.append(question)
        if not self.workiq_responses:
            return None
        response = self.workiq_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_transcript_reader_prefers_workiq_ask_json() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": '{"meetingId":"meeting-1","title":"Acme Weekly","createdDateTime":"2026-05-07T17:00:00Z","webUrl":"https://teams.microsoft.com/l/transcript/1","segments":[{"text":"Operator: Deployment velocity is improving."},{"text":"Rushi: Call out the blocker explicitly."}]}'
        }
    )
    reader = TranscriptReader(bridge)

    transcript = reader.get_transcript(meeting_id="meeting-1")

    assert len(bridge.workiq_questions) == 1
    assert bridge.calls == []
    assert transcript is not None
    assert transcript.content == "Operator: Deployment velocity is improving.\nRushi: Call out the blocker explicitly."


def test_transcript_reader_extracts_text_via_bluebird() -> None:
    bridge = _FakeBridge()
    bridge.responses.append(
        {
            "meetingId": "meeting-1",
            "title": "Acme Weekly",
            "createdDateTime": "2026-05-07T17:00:00Z",
            "webUrl": "https://teams.microsoft.com/l/transcript/1",
            "segments": [
                {"text": "Operator: Deployment velocity is improving."},
                {"text": "Rushi: Call out the blocker explicitly."},
            ],
        }
    )
    reader = TranscriptReader(bridge)

    transcript = reader.get_transcript(meeting_id="meeting-1")

    assert bridge.calls == [("workiq", "get_transcript", {"meeting_id": "meeting-1"})]
    assert transcript is not None
    assert transcript.meeting_id == "meeting-1"
    assert transcript.content == "Operator: Deployment velocity is improving.\nRushi: Call out the blocker explicitly."


def test_transcript_reader_retries_retryable_errors() -> None:
    bridge = _FakeBridge()
    bridge.responses.extend([QueryError("500"), {"id": "meeting-2", "content": "Recovered transcript"}])
    sleep_calls: list[float] = []
    reader = TranscriptReader(bridge, sleep_func=lambda delay: sleep_calls.append(delay))

    transcript = reader.get_transcript(meeting_id="meeting-2")

    assert len(bridge.calls) == 2
    assert transcript is not None
    assert transcript.content == "Recovered transcript"
    assert sleep_calls


def test_p4_20_get_transcript_by_name_returns_one_record_per_occurrence() -> None:
    """P4-20: the name-based path decodes a transcripts list into one record each."""
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": (
                '{"transcripts":['
                '{"meetingId":"m1","title":"Acme Weekly Ops Review","captured_at":"2026-06-11T17:00:00Z","content":"Operator: burn-in blocked."},'
                '{"meetingId":"m2","title":"Acme Weekly Ops Review","captured_at":"2026-06-18T17:00:00Z","content":"Operator: burn-in cleared."}'
                "]}"
            )
        }
    )
    reader = TranscriptReader(bridge)

    records = reader.get_transcript_by_name(calendar_name="Acme Weekly Ops Review", since_days=14)

    assert len(records) == 2
    assert records[0].meeting_id == "m1"
    assert records[0].content == "Operator: burn-in blocked."
    assert records[1].content == "Operator: burn-in cleared."
    # The verbatim (not summarize) instruction is present in the question.
    assert "verbatim" in bridge.workiq_questions[0]
    assert "Acme Weekly Ops Review" in bridge.workiq_questions[0]
    # No MCP fallback path is taken on the name-based path.
    assert bridge.calls == []


def test_p4_20_get_transcript_by_name_drops_empty_content_records() -> None:
    """Occurrences with no transcript text are dropped, not returned as empty records."""
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {"transcripts": [{"meetingId": "m1", "content": "valid text"}, {"meetingId": "m2", "content": ""}]}
    )
    reader = TranscriptReader(bridge)

    records = reader.get_transcript_by_name(calendar_name="Acme Weekly Ops Review", since_days=7)

    assert len(records) == 1
    assert records[0].meeting_id == "m1"


def test_p4_20_get_transcript_by_name_degrades_gracefully() -> None:
    """Empty calendar name → no call; empty/None response → (); transport error → ()."""
    bridge = _FakeBridge()
    reader = TranscriptReader(bridge)

    # Blank name short-circuits before touching the bridge.
    assert reader.get_transcript_by_name(calendar_name="   ", since_days=7) == ()
    assert bridge.workiq_questions == []

    # No queued response → ask_workiq returns None → ().
    assert reader.get_transcript_by_name(calendar_name="Acme Weekly Ops Review", since_days=7) == ()

    # Transport error degrades to () rather than raising.
    bridge.workiq_responses.append(QueryError("transient"))
    assert reader.get_transcript_by_name(calendar_name="Acme Weekly Ops Review", since_days=7) == ()
