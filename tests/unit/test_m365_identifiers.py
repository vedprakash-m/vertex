from __future__ import annotations

from src.core.m365_identifiers import normalize_meeting_id, normalize_thread_id


def test_normalize_meeting_id_compacts_numeric_meeting_code() -> None:
    assert normalize_meeting_id("258 356 881 302 011") == "258356881302011"


def test_normalize_meeting_id_extracts_short_teams_meet_link() -> None:
    assert (
        normalize_meeting_id("https://teams.microsoft.com/meet/258356881302011?p=LOPGasWbahdOPtbWK9")
        == "258356881302011"
    )


def test_normalize_meeting_id_extracts_meetup_join_path_identifier() -> None:
    assert (
        normalize_meeting_id(
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_OTNhZjI2MjAtMjg4Yi00NzU2LWE0NjQtMjZjNDdiZTdiYzJl%40thread.v2/0"
        )
        == "19:meeting_OTNhZjI2MjAtMjg4Yi00NzU2LWE0NjQtMjZjNDdiZTdiYzJl@thread.v2"
    )


def test_normalize_thread_id_extracts_chat_path_identifier() -> None:
    assert (
        normalize_thread_id(
            "https://teams.microsoft.com/l/chat/19:8c5eec9e0e4f4daa8b5e8c32fec26b7a@thread.v2/conversations?context=%7B%22contextType%22%3A%22chat%22%7D"
        )
        == "19:8c5eec9e0e4f4daa8b5e8c32fec26b7a@thread.v2"
    )


def test_normalize_thread_id_extracts_meeting_chat_path_identifier() -> None:
    assert (
        normalize_thread_id(
            "https://teams.microsoft.com/l/chat/19:meeting_OTlmZmQ4OGEtMWExYS00N2VhLTk5YjktZmY3Y2I5NzM5ZDk5@thread.v2/conversations?context=%7B%22contextType%22%3A%22chat%22%7D"
        )
        == "19:meeting_OTlmZmQ4OGEtMWExYS00N2VhLTk5YjktZmY3Y2I5NzM5ZDk5@thread.v2"
    )
