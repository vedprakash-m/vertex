"""Tests for BL-19 (mail body_text) and BL-26 (transcript body_text)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.m365.enricher import _mail_enrichment, _transcript_enrichment, _MAIL_PREVIEW_MAX_CHARS, _TRANSCRIPT_BODY_MAX_CHARS
from src.core.models import Enrichment


def _mail_record(**kwargs) -> MagicMock:
    defaults = dict(
        source_id="msg:ABC123",
        subject="Test Subject",
        sender="sender@test.com",
        received_at="2026-06-03T23:34:00Z",
        web_url="https://outlook.com/msg/ABC123",
        preview=None,
    )
    defaults.update(kwargs)
    r = MagicMock()
    for k, v in defaults.items():
        setattr(r, k, v)
    return r


def _transcript_record(**kwargs) -> MagicMock:
    defaults = dict(
        meeting_id="meet:XYZ789",
        title="Acme Daily Standup",
        captured_at="2026-06-16T15:00:00Z",
        web_url="https://teams.microsoft.com/meeting/XYZ789",
        content="",
    )
    defaults.update(kwargs)
    r = MagicMock()
    for k, v in defaults.items():
        setattr(r, k, v)
    return r


_AS_OF = datetime(2026, 6, 17, tzinfo=timezone.utc)


# ── BL-19: mail body_text ──────────────────────────────────────────────────────

def test_mail_enrichment_captures_preview():
    record = _mail_record(preview="Performance is HIGH; ADO 34705323 blocking.")
    result = _mail_enrichment(record)
    assert result is not None
    assert result.body_text == "Performance is HIGH; ADO 34705323 blocking."


def test_mail_enrichment_no_preview_leaves_body_text_none():
    record = _mail_record(preview=None)
    result = _mail_enrichment(record)
    assert result is not None
    assert result.body_text is None


def test_mail_enrichment_whitespace_only_preview_leaves_body_text_none():
    record = _mail_record(preview="   \n  ")
    result = _mail_enrichment(record)
    assert result is not None
    assert result.body_text is None


def test_mail_enrichment_truncates_long_preview():
    long_preview = "x" * (_MAIL_PREVIEW_MAX_CHARS + 500)
    record = _mail_record(preview=long_preview)
    result = _mail_enrichment(record)
    assert result is not None
    assert len(result.body_text) == _MAIL_PREVIEW_MAX_CHARS


def test_mail_enrichment_excerpt_still_has_subject():
    record = _mail_record(subject="[DD Acme] Scorecard 06/03", preview="Body text here.")
    result = _mail_enrichment(record)
    assert result is not None
    assert result.excerpt == "Subject: [DD Acme] Scorecard 06/03"


def test_mail_enrichment_returns_none_when_no_source_id():
    record = _mail_record(source_id=None, web_url=None)
    assert _mail_enrichment(record) is None


# ── BL-26: transcript body_text ────────────────────────────────────────────────

def test_transcript_enrichment_captures_content():
    content = "Executive Reader: We have to go back and report to leadership and partner teams..."
    record = _transcript_record(content=content)
    result = _transcript_enrichment(record, as_of=_AS_OF)
    assert result is not None
    assert result.body_text == content


def test_transcript_enrichment_no_content_leaves_body_text_none():
    record = _transcript_record(content="")
    result = _transcript_enrichment(record, as_of=_AS_OF)
    assert result is not None
    assert result.body_text is None


def test_transcript_enrichment_whitespace_only_content_leaves_none():
    record = _transcript_record(content="   \n  ")
    result = _transcript_enrichment(record, as_of=_AS_OF)
    assert result is not None
    assert result.body_text is None


def test_transcript_enrichment_truncates_long_content():
    long_content = "A" * (_TRANSCRIPT_BODY_MAX_CHARS + 1000)
    record = _transcript_record(content=long_content)
    result = _transcript_enrichment(record, as_of=_AS_OF)
    assert result is not None
    assert len(result.body_text) == _TRANSCRIPT_BODY_MAX_CHARS


def test_transcript_enrichment_excerpt_uses_transcript_prefix():
    record = _transcript_record(title="Acme Ramp Standup")
    result = _transcript_enrichment(record, as_of=_AS_OF)
    assert result is not None
    assert result.excerpt == "Transcript: Acme Ramp Standup"


def test_transcript_enrichment_returns_none_when_no_source_id():
    record = _transcript_record(meeting_id=None, web_url=None)
    assert _transcript_enrichment(record, as_of=_AS_OF) is None


# ── Enrichment model: body_text field ──────────────────────────────────────────

def test_enrichment_body_text_defaults_to_none():
    e = Enrichment(
        source="mail",
        source_id="id1",
        author="user",
        timestamp=_AS_OF,
        excerpt="Subject: test",
        permalink=None,
    )
    assert e.body_text is None


def test_enrichment_body_text_can_be_set():
    e = Enrichment(
        source="transcript",
        source_id="id2",
        author="meeting transcript",
        timestamp=_AS_OF,
        excerpt="Transcript: standup",
        permalink=None,
        body_text="Verbatim content here.",
    )
    assert e.body_text == "Verbatim content here."


def test_enrichment_accepts_local_kb_source():
    e = Enrichment(
        source="local_kb",
        source_id="~/OneDrive/acme/onedeploy.md",
        author="local_file",
        timestamp=_AS_OF,
        excerpt="onedeploy.md",
        permalink=None,
        body_text="Tao Peng owns 7 RAMPP1 items.",
    )
    assert e.source == "local_kb"
