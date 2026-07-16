"""ADF-W3.7 remainder: unit tests for src/core/context_gap_reply_import.py."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from src.core.context_gap_reply_import import list_pending_replies, parse_reply_eml, replies_dir
from src.core.eml_writer import write_eml_atomic


def _write_reply_eml(path: Path, *, from_addr: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = "vertex@example.com"
    message["Subject"] = subject
    message.set_content(body)
    write_eml_atomic(path, eml_bytes=message.as_bytes())


def test_replies_dir_is_under_nudge(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert replies_dir("xpf", programs_root=programs_root) == programs_root / "xpf" / "nudge" / "replies"


def test_list_pending_replies_empty_when_directory_absent(tmp_path: Path) -> None:
    assert list_pending_replies("xpf", programs_root=tmp_path / "programs") == ()


def test_list_pending_replies_finds_eml_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    directory = replies_dir("xpf", programs_root=programs_root)
    directory.mkdir(parents=True)
    _write_reply_eml(directory / "reply1.eml", from_addr="alex@example.com", subject="Re: gap", body="answer")
    _write_reply_eml(directory / "reply2.eml", from_addr="priya@example.com", subject="Re: gap", body="answer 2")

    found = list_pending_replies("xpf", programs_root=programs_root)
    assert len(found) == 2
    assert all(path.suffix == ".eml" for path in found)


def test_parse_reply_eml_extracts_sender_and_subject(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="Alex Example <alex@example.com>", subject="Re: [Vertex] Missing info", body="The answer is X.\n\nReference: solicitation-1\n")

    parsed = parse_reply_eml(path)
    assert parsed.sender_email == "alex@example.com"
    assert parsed.subject == "Re: [Vertex] Missing info"
    assert parsed.reference_marker == "solicitation-1"


def test_parse_reply_eml_isolates_new_content_above_original_message_marker(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = (
        "The answer is X.\n\n"
        "Reference: solicitation-1\n\n"
        "-----Original Message-----\n"
        "From: vertex@example.com\n"
        "Subject: [Vertex] Missing info needed\n\n"
        "Hi Alex,\n\nWhy does this workstream exist?\n"
    )
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert "The answer is X." in parsed.body_text
    assert "Why does this workstream exist" not in parsed.body_text


def test_parse_reply_eml_isolates_new_content_above_on_wrote_marker(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = (
        "The answer is Y.\n\n"
        "Reference: solicitation-2\n\n"
        "On Mon, Jul 1, 2026 at 9:00 AM Vertex <vertex@example.com> wrote:\n"
        "> Why does this workstream exist?\n"
    )
    _write_reply_eml(path, from_addr="priya@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert "The answer is Y." in parsed.body_text
    assert "Why does this workstream exist" not in parsed.body_text


def test_parse_reply_eml_reference_marker_survives_quote_prefix(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    body = "My answer.\n\n> Reference: solicitation-3\n"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body=body)

    parsed = parse_reply_eml(path)
    assert parsed.reference_marker == "solicitation-3"


def test_parse_reply_eml_no_reference_marker_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body="just an answer, no marker")

    parsed = parse_reply_eml(path)
    assert parsed.reference_marker is None


def test_parse_reply_eml_no_separator_keeps_full_body(tmp_path: Path) -> None:
    path = tmp_path / "reply.eml"
    _write_reply_eml(path, from_addr="alex@example.com", subject="Re: gap", body="Just a plain answer with no quoted content.")

    parsed = parse_reply_eml(path)
    assert parsed.body_text == "Just a plain answer with no quoted content."
