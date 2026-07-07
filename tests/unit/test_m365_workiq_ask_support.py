from __future__ import annotations

import pytest
from datetime import date

from src.m365.workiq_ask_support import (
    build_calendar_search_question,
    build_structured_discovery_question,
    build_mail_search_question,
    build_teams_search_question,
    build_transcript_question,
    coerce_workiq_json_payload,
    extract_json_value_from_text,
    extract_source_url_contexts,
    extract_source_urls_from_text,
    validate_structured_discovery_payload,
    DiscoveryRequest,
)


# ---------------------------------------------------------------------------
# coerce_workiq_json_payload
# ---------------------------------------------------------------------------


def test_coerce_returns_none_when_input_is_none() -> None:
    assert coerce_workiq_json_payload(None) is None


def test_coerce_returns_structured_dict_unchanged_when_root_key_present() -> None:
    payload = {"emails": [{"id": "1"}], "next_cursor": None}
    result = coerce_workiq_json_payload(payload, root_key="emails")
    assert result is payload


def test_coerce_extracts_embedded_json_from_prose_response_key() -> None:
    payload = {"response": '{"emails":[{"id":"abc","subject":"Budget"}],"next_cursor":null}'}
    result = coerce_workiq_json_payload(payload, root_key="emails")
    assert result is not None
    assert result["emails"][0]["id"] == "abc"


def test_coerce_extracts_json_from_markdown_code_block_in_response() -> None:
    payload = {"response": "Here are results:\n```json\n{\"events\":[{\"id\":\"evt-1\"}]}\n```"}
    result = coerce_workiq_json_payload(payload, root_key="events")
    assert result is not None
    assert result["events"][0]["id"] == "evt-1"


def test_coerce_returns_original_payload_when_no_json_found_in_prose() -> None:
    payload = {"response": "I found no meetings related to that topic."}
    result = coerce_workiq_json_payload(payload, root_key="events")
    # Cannot parse JSON → returns original payload (prose extraction downstream)
    assert result is payload


def test_coerce_wraps_list_in_root_key_dict() -> None:
    payload = {"response": '[{"id":"m1"},{"id":"m2"}]'}
    result = coerce_workiq_json_payload(payload, root_key="messages")
    assert result is not None
    assert len(result["messages"]) == 2


def test_coerce_no_root_key_returns_non_prose_dict_unchanged() -> None:
    payload = {"meetingId": "abc", "content": "transcript text but not a prose key trigger"}
    # root_key=None and no prose wrapper keys → return as-is
    result = coerce_workiq_json_payload(payload)
    assert result is payload


# ---------------------------------------------------------------------------
# extract_json_value_from_text
# ---------------------------------------------------------------------------


def test_extract_json_value_from_text_parses_object() -> None:
    assert extract_json_value_from_text('{"key":"val"}') == {"key": "val"}


def test_extract_json_value_from_text_parses_array() -> None:
    result = extract_json_value_from_text('[{"id":1},{"id":2}]')
    assert result == [{"id": 1}, {"id": 2}]


def test_extract_json_value_from_text_extracts_embedded_object() -> None:
    result = extract_json_value_from_text('Some prose {"k":"v"} trailing text')
    assert result == {"k": "v"}


def test_extract_json_value_from_text_extracts_json_code_block() -> None:
    result = extract_json_value_from_text("```json\n{\"x\":1}\n```")
    assert result == {"x": 1}


def test_extract_json_value_from_text_returns_none_on_empty() -> None:
    assert extract_json_value_from_text("") is None
    assert extract_json_value_from_text("   ") is None


def test_extract_json_value_from_text_returns_none_on_no_json() -> None:
    assert extract_json_value_from_text("No meetings found this week.") is None


# ---------------------------------------------------------------------------
# extract_source_urls_from_text / extract_source_url_contexts
# ---------------------------------------------------------------------------


def test_extract_source_urls_returns_empty_tuple_for_blank_text() -> None:
    assert extract_source_urls_from_text(None) == ()
    assert extract_source_urls_from_text("") == ()


def test_extract_source_urls_finds_https_urls() -> None:
    text = "See https://teams.microsoft.com/l/meeting/123 for details."
    urls = extract_source_urls_from_text(text)
    assert len(urls) == 1
    assert urls[0] == "https://teams.microsoft.com/l/meeting/123"


def test_extract_source_urls_trims_trailing_punctuation() -> None:
    text = "Link: https://outlook.office.com/calendar/item/ABC."
    urls = extract_source_urls_from_text(text)
    assert urls[0].endswith("ABC")


def test_extract_source_urls_deduplicates() -> None:
    url = "https://teams.microsoft.com/l/meeting/xyz"
    text = f"First {url} then again {url}."
    urls = extract_source_urls_from_text(text)
    assert urls.count(url) == 1


def test_extract_source_url_contexts_returns_empty_for_none() -> None:
    assert extract_source_url_contexts(None) == ()


def test_extract_source_url_contexts_pairs_url_with_prose_context() -> None:
    text = "Teams Weekly Sync https://teams.microsoft.com/l/meeting/abc"
    contexts = dict(extract_source_url_contexts(text))
    url = "https://teams.microsoft.com/l/meeting/abc"
    assert url in contexts
    assert "Teams Weekly Sync" in contexts[url]


# ---------------------------------------------------------------------------
# Question builders — structural contract
# ---------------------------------------------------------------------------


def test_build_mail_search_question_contains_json_schema_hint() -> None:
    q = build_mail_search_question(query="budget review", limit=10)
    assert "emails" in q
    assert "budget review" in q
    assert "10" in q


def test_build_mail_search_question_includes_cursor_hint_when_provided() -> None:
    q = build_mail_search_question(query="sync", limit=5, cursor="tok-42")
    assert "tok-42" in q


def test_build_calendar_search_question_contains_json_schema_hint() -> None:
    q = build_calendar_search_question(query="quarterly planning", limit=15)
    assert "events" in q
    assert "quarterly planning" in q
    assert "15" in q


def test_build_teams_search_question_includes_channel_name() -> None:
    q = build_teams_search_question(channel="xInfraSWPM: Acme Weekly", query="velocity", since=None, limit=10)
    assert "xInfraSWPM: Acme Weekly" in q
    assert "velocity" in q


def test_build_teams_search_question_uses_any_channel_for_all() -> None:
    q = build_teams_search_question(channel="all", query="deploy", since="2026-06-01", limit=5)
    assert "any channel or chat" in q
    assert "2026-06-01" in q


def test_build_transcript_question_includes_meeting_id() -> None:
    q = build_transcript_question(meeting_id="mtg-abc-123")
    assert "mtg-abc-123" in q
    assert "transcript" in q.lower()


def test_build_structured_discovery_question_is_deterministic_and_windowed() -> None:
    request = DiscoveryRequest(
        lane_name="Acme Ramp",
        terms=("Northwind", "launch readiness"),
        window_start=date(2026, 6, 6),
        window_end=date(2026, 6, 20),
    )

    first = build_structured_discovery_question(request)
    second = build_structured_discovery_question(request)

    assert first == second
    assert "between 2026-06-06 and 2026-06-20" in first
    assert "Northwind; launch readiness" in first
    assert 'return {"emails":[]}' in first


def test_build_structured_discovery_question_rejects_control_sequences() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        build_structured_discovery_question(
            DiscoveryRequest(
                lane_name="Acme\x1b[31m",
                terms=("Northwind",),
                window_start=date(2026, 6, 6),
                window_end=date(2026, 6, 20),
            )
        )


def test_validate_structured_discovery_payload_filters_and_bounds_records() -> None:
    payload = {
        "response": """{"emails":[
            {"id":"mail-1","conversationId":"thread-1","subject":"Valid","receivedDateTime":"2026-06-10T08:00:00Z","webUrl":"https://outlook.office.com/mail/deeplink/read/1","bodyPreview":"Ready"},
            {"id":"mail-2","conversationId":"thread-2","subject":"Outside","receivedDateTime":"2026-05-01T08:00:00Z","webUrl":"https://outlook.office.com/mail/deeplink/read/2"},
            {"id":"mail-3","conversationId":"thread-3","subject":"Bad host","receivedDateTime":"2026-06-11T08:00:00Z","webUrl":"https://evil.example/steal"},
            {"id":"mail-4","conversationId":"thread-4","subject":"Unsafe\\u001b[2J","receivedDateTime":"2026-06-12T08:00:00Z"}
        ]}"""
    }

    result = validate_structured_discovery_payload(
        payload,
        window_start=date(2026, 6, 6),
        window_end=date(2026, 6, 20),
        limit=1,
    )

    assert [record["conversationId"] for record in result["emails"]] == ["thread-1"]
    assert result["emails"][0]["receivedDateTime"] == "2026-06-10T08:00:00Z"


def test_validate_structured_discovery_payload_synthesizes_identity_for_naive_iso_timestamp() -> None:
    result = validate_structured_discovery_payload(
        {
            "emails": [
                {
                    "id": "turn1search2",
                    "subject": "Naive timestamp",
                    "from": "owner@example.com",
                    "receivedDateTime": "2026-06-10T08:00:00",
                },
                {"subject": "Missing sender", "receivedDateTime": "2026-06-10T08:00:00Z"},
            ]
        },
        window_start=date(2026, 6, 6),
        window_end=date(2026, 6, 20),
        limit=8,
    )

    assert len(result["emails"]) == 1
    assert result["emails"][0]["id"].startswith("semantic:")
    assert result["emails"][0]["receivedDateTime"] == "2026-06-10T08:00:00Z"
    assert "conversationId" not in result["emails"][0]


def test_validate_structured_discovery_payload_deduplicates_semantic_identity() -> None:
    record = {
        "id": "turn1search1",
        "subject": "Ramp update",
        "from": "owner@example.com",
        "receivedDateTime": "2026-06-10T08:00:00",
    }
    duplicate = {**record, "id": "turn1search9"}

    result = validate_structured_discovery_payload(
        {"emails": [record, duplicate]},
        window_start=date(2026, 6, 6),
        window_end=date(2026, 6, 20),
        limit=8,
    )

    assert len(result["emails"]) == 1


def test_extract_json_value_from_text_recovers_cli_wrapped_json() -> None:
    wrapped = '{"emails":[{"id":"turn1sea\nrch1","subject":"Ramp\n update"}]}'

    assert extract_json_value_from_text(wrapped) == {
        "emails": [{"id": "turn1search1", "subject": "Ramp update"}]
    }
