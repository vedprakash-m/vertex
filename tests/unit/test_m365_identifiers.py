from __future__ import annotations

from src.core.m365_identifiers import normalize_meeting_id, normalize_thread_id


def test_normalize_meeting_id_compacts_numeric_meeting_code() -> None:
    assert normalize_meeting_id("123 456 789 012 345") == "123456789012345"


def test_normalize_meeting_id_extracts_short_teams_meet_link() -> None:
    assert (
        normalize_meeting_id("https://teams.microsoft.com/meet/123456789012345?p=fixture-passcode")
        == "123456789012345"
    )


def test_normalize_meeting_id_extracts_meetup_join_path_identifier() -> None:
    assert (
        normalize_meeting_id(
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_MDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAx%40thread.v2/0"
        )
        == "19:meeting_MDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAx@thread.v2"
    )


def test_normalize_thread_id_extracts_chat_path_identifier() -> None:
    assert (
        normalize_thread_id(
            "https://teams.microsoft.com/l/chat/19:00000000000000000000000000000000@thread.v2/conversations?context=%7B%22contextType%22%3A%22chat%22%7D"
        )
        == "19:00000000000000000000000000000000@thread.v2"
    )


def test_normalize_thread_id_extracts_meeting_chat_path_identifier() -> None:
    assert (
        normalize_thread_id(
            "https://teams.microsoft.com/l/chat/19:meeting_MDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAy@thread.v2/conversations?context=%7B%22contextType%22%3A%22chat%22%7D"
        )
        == "19:meeting_MDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAy@thread.v2"
    )
