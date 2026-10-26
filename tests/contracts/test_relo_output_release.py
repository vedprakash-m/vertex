from __future__ import annotations

from datetime import datetime, timezone
from email import message_from_bytes
from types import SimpleNamespace

from src.commands.report_email import _build_preview_eml_bytes


def _bundle() -> object:
    return SimpleNamespace(
        config=SimpleNamespace(
            author=SimpleNamespace(email="owner@example.test", display_name="Owner"),
            edition=SimpleNamespace(name="Weekly"),
        )
    )


def test_blocked_draft_has_no_recipients_and_visible_gate_reason() -> None:
    blocked = message_from_bytes(
        _build_preview_eml_bytes(
            _bundle(), issue_number=1, as_of=datetime.now(timezone.utc), html_body="<p>body</p>",
            markdown_body="body", suggested_subject="Status", generated_at=datetime.now(timezone.utc),
            blocking_gate_ids=("QG-01",),
        )
    )
    clean = message_from_bytes(
        _build_preview_eml_bytes(
            _bundle(), issue_number=1, as_of=datetime.now(timezone.utc), html_body="<p>body</p>",
            markdown_body="body", suggested_subject="Status", generated_at=datetime.now(timezone.utc),
        )
    )
    assert blocked["Subject"].startswith("[BLOCKED DRAFT]")
    assert blocked["To"] is None and blocked["Cc"] is None
    assert "QG-01" in blocked.get_payload(0).get_payload(decode=True).decode()
    assert clean["To"] == "owner@example.test"
