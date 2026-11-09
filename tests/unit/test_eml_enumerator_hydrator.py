"""Unit tests for EmlEnumerator (P1-1) and EmlHydrator (P1-2).

The inline message examples are authored synthetic fixtures.

Covers:
* EmlEnumerator: 3-dir atomicity, FIFO ordering, crash recovery, limit, lock contention, OSError fallback
* EmlHydrator: MIME plain-text, HTML stripping, empty body, attachment denied, missing path
* End-to-end: enumerator + hydrator produce HydratedContent from .eml fixture
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import textwrap
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, Success, Unsupported
from src.m365.rev.eml_enumerator import EmlEnumerator
from src.m365.rev import eml_hydrator
from src.m365.rev.eml_hydrator import EmlHydrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW_ISO = "2026-06-24T10:00:00+00:00"

_SIMPLE_EML = """\
From: Example Sender <sender@fixture.example.net>
To: recipient@fixture.example.net
Subject: Example deployment completed
Message-ID: <plain-id@fixture.example.net>
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Example deployment completed for the synthetic sample.
All sample devices passed their checks.
"""

_HTML_EML = """\
From: Example Sender <sender@fixture.example.net>
To: recipient@fixture.example.net
Subject: Example milestone
Message-ID: <html-id@fixture.example.net>
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><p>Milestone <b>complete</b>.</p><script>alert('not included in extracted text')</script></body></html>
"""

_QUOTED_REPLY_EML = """\
From: Example Responder <responder@fixture.example.net>
To: recipient@fixture.example.net
Subject: Re: Example deployment
Message-ID: <reply-id@fixture.example.net>
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Thanks for the update.

On Wed, 24 Jun 2026, Example Sender <sender@fixture.example.net> wrote:
> Example deployment completed.
"""

_NO_MSGID_EML = """\
From: anonymous@fixture.example.net
To: recipient@fixture.example.net
Subject: Example without an ID
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

A synthetic message without a Message-ID.
"""

_ATTACHMENT_EML = """\
From: service@fixture.example.net
To: recipient@fixture.example.net
Subject: Example attachment
Message-ID: <attachment-id@fixture.example.net>
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="fixture-boundary"

--fixture-boundary
Content-Type: text/plain; charset=utf-8

A synthetic message with an attachment.
--fixture-boundary
Content-Type: application/pdf
Content-Disposition: attachment; filename="sample.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--fixture-boundary--
"""

_EMPTY_BODY_EML = """\
From: service@fixture.example.net
To: recipient@fixture.example.net
Subject: Empty example
Message-ID: <empty-id@fixture.example.net>
Date: Wed, 24 Jun 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

"""


def _write_eml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _make_intent(tenant_id: str = "t", mailbox: str = "fixture-user-01@group-01.fixture.example.net") -> RetrievalIntent:
    from src.core.rev.query_planner import RetrievalIntent
    from src.core.rev.entity_types import EntityType
    return RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        limit=10,
    )


def _make_candidate(eml_path: Path, message_id: str) -> EnumeratedCandidate:
    return EnumeratedCandidate(
        locator=HydrationLocator(
            source_type=EntityType.MESSAGE,
            tenant_id="tenant-test",
            principal_mailbox="recipient@fixture.example.net",
            container="inbox",
            resource_id=message_id,
        ),
        relevance_score=0.9,
        partial_metadata={
            "eml_path": str(eml_path),
            "message_id": message_id,
            "is_recovery": False,
            "claimed_at": NOW_ISO,
        },
        correlation_id="test-cid",
        enumerator="eml_local",
    )


# ---------------------------------------------------------------------------
# EmlEnumerator tests
# ---------------------------------------------------------------------------


class TestEmlEnumerator:
    def _enumerator(self, inbox: Path) -> EmlEnumerator:
        return EmlEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="tenant-test",
            principal_mailbox="recipient@fixture.example.net",
        )

    def test_empty_inbox_returns_empty_success(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c1")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_single_file_enumerated_and_claimed(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "msg001.eml", _SIMPLE_EML)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c1")
        assert isinstance(result, Success)
        assert len(result.value) == 1
        candidate = result.value[0]
        assert candidate.enumerator == "eml_local"
        assert candidate.locator.resource_id == "<plain-id@fixture.example.net>"
        # File should be in claimed/ now.
        claimed_files = list(enum.claimed_dir().glob("*.eml"))
        assert len(claimed_files) == 1
        # Original should no longer be in inbox/
        assert not (inbox / "msg001.eml").exists()

    def test_multiple_files_fifo_ordering(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # Write 3 files with different mtimes.
        for i, (name, content) in enumerate([
            ("a.eml", _SIMPLE_EML),
            ("b.eml", _HTML_EML),
            ("c.eml", _QUOTED_REPLY_EML),
        ]):
            p = inbox / name
            _write_eml(p, content)
            import os
            os.utime(p, (1000 + i, 1000 + i))  # mtime 1000, 1001, 1002

        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c2")
        assert isinstance(result, Success)
        assert len(result.value) == 3
        # Ordering by mtime: a.eml (1000) < b.eml (1001) < c.eml (1002)
        paths = [c.partial_metadata["eml_path"] for c in result.value]
        assert "a.eml" in paths[0]
        assert "b.eml" in paths[1]
        assert "c.eml" in paths[2]

    def test_crash_recovery_claimed_files_surface_first(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        enum = self._enumerator(inbox)
        # Put a file directly in claimed/ (simulates prior crash).
        claimed = enum.claimed_dir()
        claimed.mkdir(parents=True, exist_ok=True)
        _write_eml(claimed / "crash.eml", _SIMPLE_EML)
        # And put a new file in inbox/.
        _write_eml(inbox / "new.eml", _HTML_EML)
        result = enum.enumerate(_make_intent(), correlation_id="c3")
        assert isinstance(result, Success)
        assert len(result.value) == 2
        # Recovery file must come first.
        assert "crash.eml" in result.value[0].partial_metadata["eml_path"]
        assert result.value[0].relevance_score == 1.0  # recovery gets 1.0
        assert result.value[1].relevance_score == 0.9  # new file gets 0.9

    def test_limit_respected_returns_incomplete(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(5):
            _write_eml(inbox / f"msg{i:03d}.eml", _SIMPLE_EML.replace(
                "<plain-id@fixture.example.net>", f"<test-{i:03d}@example.com>"
            ))
        enum = EmlEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="fixture-user-01@group-01.fixture.example.net",
            limit=3,
        )
        result = enum.enumerate(_make_intent(), correlation_id="c4")
        assert isinstance(result, Incomplete)
        assert len(result.value) == 3

    def test_mark_processed_moves_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "done.eml", _SIMPLE_EML)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c5")
        assert isinstance(result, Success)
        eml_path = result.value[0].partial_metadata["eml_path"]
        enum.mark_processed(eml_path)
        assert not Path(eml_path).exists()
        assert (enum.processed_dir() / "done.eml").exists()

    def test_mark_quarantined_moves_file_with_reason(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "bad.eml", _EMPTY_BODY_EML)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c6")
        assert isinstance(result, Success)
        eml_path = result.value[0].partial_metadata["eml_path"]
        enum.mark_quarantined(eml_path, reason="body_empty")
        assert not Path(eml_path).exists()
        quarantine_dir = enum.quarantine_dir()
        assert (quarantine_dir / "bad.eml").exists()
        reason_file = quarantine_dir / "bad.reason.txt"
        assert reason_file.exists()
        assert "body_empty" in reason_file.read_text()

    def test_no_message_id_synthesizes_stable_id(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "nomid.eml", _NO_MSGID_EML)
        enum = self._enumerator(inbox)
        result = enum.enumerate(_make_intent(), correlation_id="c7")
        assert isinstance(result, Success)
        resource_id = result.value[0].locator.resource_id
        assert resource_id.startswith("sha256:")
        # Stable: same content → same id
        result2_enum = self._enumerator(tmp_path / "inbox2")
        (tmp_path / "inbox2").mkdir(parents=True)
        _write_eml(tmp_path / "inbox2" / "nomid2.eml", _NO_MSGID_EML)
        result2 = result2_enum.enumerate(_make_intent(), correlation_id="c7b")
        assert isinstance(result2, Success)
        assert result2.value[0].locator.resource_id == resource_id


# ---------------------------------------------------------------------------
# EmlHydrator tests
# ---------------------------------------------------------------------------


class TestEmlHydrator:
    def _hydrator(self, *, attachment_denied: Path | None = None) -> EmlHydrator:
        return EmlHydrator(
            mailbox_tenant_id="tenant-test",
            principal_mailbox="recipient@fixture.example.net",
            attachment_denied_path=attachment_denied,
        )

    def _candidate(self, path: Path, message_id: str = "<plain-id@fixture.example.net>") -> EnumeratedCandidate:
        return _make_candidate(path, message_id)

    def test_plain_text_eml_hydrated(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "msg.eml", _SIMPLE_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(self._candidate(p), correlation_id="h1")
        assert isinstance(result, Success)
        content = result.value
        assert isinstance(content, HydratedContent)
        assert "deployment completed" in content.canonical_text.lower()
        assert content.metadata_only is False
        assert content.chunks

    def test_html_eml_stripped(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "html.eml", _HTML_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(
            self._candidate(p, "<html-id@fixture.example.net>"), correlation_id="h2"
        )
        assert isinstance(result, Success)
        text = result.value.canonical_text
        assert "milestone" in text.lower() or "complete" in text.lower()
        # Script tag content must be stripped
        assert "alert" not in text

    def test_quoted_reply_stripped(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "qr.eml", _QUOTED_REPLY_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(
            self._candidate(p, "<reply-id@fixture.example.net>"), correlation_id="h3"
        )
        assert isinstance(result, Success)
        text = result.value.canonical_text
        assert "thanks" in text.lower()
        # Quoted reply content should be stripped (or minimized)
        assert text.count("sender@fixture.example.net") == 0 or ">" not in text

    def test_empty_body_is_metadata_only(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "empty.eml", _EMPTY_BODY_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(
            self._candidate(p, "<empty-id@fixture.example.net>"), correlation_id="h4"
        )
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_attachment_denied_logged(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "attach.eml", _ATTACHMENT_EML)
        denied_log = tmp_path / "attachment_denied.jsonl"
        hydrator = self._hydrator(attachment_denied=denied_log)
        result = hydrator.hydrate(
            self._candidate(p, "<attachment-id@fixture.example.net>"), correlation_id="h5"
        )
        assert isinstance(result, Success)
        # attachment_denied.jsonl should have an entry
        assert denied_log.exists()
        records = [json.loads(line) for line in denied_log.read_text().splitlines() if line.strip()]
        assert len(records) >= 1
        assert records[0]["message_id"] == "<attachment-id@fixture.example.net>"
        assert "application/pdf" in records[0]["denied_content_types"]

    def test_missing_eml_path_returns_unsupported(self, tmp_path: Path) -> None:
        hydrator = self._hydrator()
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="fixture-user-01@group-01.fixture.example.net",
                container="inbox",
                resource_id="<fixture-user-12@group-01.fixture.example.net>",
            ),
            relevance_score=0.9,
            partial_metadata={},  # no eml_path
            correlation_id="hx",
            enumerator="eml_local",
        )
        result = hydrator.hydrate(candidate, correlation_id="hx")
        assert isinstance(result, Unsupported)
        assert "eml_path_missing" in result.reason

    def test_nonexistent_path_returns_unsupported(self, tmp_path: Path) -> None:
        hydrator = self._hydrator()
        candidate = _make_candidate(
            tmp_path / "nonexistent.eml", "<fixture-user-13@group-01.fixture.example.net>"
        )
        result = hydrator.hydrate(candidate, correlation_id="hy")
        assert isinstance(result, Unsupported)
        assert result.reason == "eml_not_found: local source is unavailable"
        assert str(tmp_path) not in result.reason

    @pytest.mark.parametrize("timeout", [False, True])
    def test_hydration_failure_diagnostics_omit_private_details(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, timeout: bool,
    ) -> None:
        path = _write_eml(tmp_path / "synthetic-private-name.eml", _SIMPLE_EML)
        hydrator = self._hydrator()
        marker = "SYNTHETIC_PRIVATE_EXCEPTION_CONTENT"

        def fail(*args: object, **kwargs: object) -> None:
            if timeout:
                raise eml_hydrator._TimeoutError(marker)
            raise OSError(marker)

        monkeypatch.setattr(hydrator, "_hydrate_path", fail)
        with caplog.at_level(logging.WARNING, logger=eml_hydrator.__name__):
            result = hydrator.hydrate(self._candidate(path), correlation_id="h1")

        assert isinstance(result, Unsupported)
        if timeout:
            assert result.reason == "hydration_timeout: exceeded 30s"
            assert "timeout after 30s" in caplog.text
        else:
            assert result.reason == "hydration_error: source could not be hydrated"
            assert "failed to hydrate" in caplog.text
        assert marker not in result.reason
        assert marker not in caplog.text
        assert str(path) not in result.reason
        assert str(path) not in caplog.text

    def test_attachment_warning_omits_private_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _write_eml(tmp_path / "attachment.eml", _ATTACHMENT_EML)
        denied_path = tmp_path / "synthetic-private-dir" / "attachment_denied.jsonl"
        hydrator = self._hydrator(attachment_denied=denied_path)
        marker = "SYNTHETIC_PRIVATE_EXCEPTION_CONTENT"

        def fail(*args: object, **kwargs: object) -> None:
            raise OSError(f"{marker}: {denied_path}")

        monkeypatch.setattr(eml_hydrator, "append_jsonl_line", fail)
        with caplog.at_level(logging.WARNING, logger=eml_hydrator.__name__):
            result = hydrator.hydrate(
                self._candidate(path, "<attachment-id@fixture.example.net>"), correlation_id="h5"
            )

        assert isinstance(result, Success)
        assert "synthetic message with an attachment" in result.value.canonical_text.lower()
        assert "could not write attachment_denied.jsonl" in caplog.text
        assert marker not in caplog.text
        assert str(denied_path) not in caplog.text

    def test_html_fallback_omits_private_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _write_eml(tmp_path / "html.eml", _HTML_EML)
        hydrator = self._hydrator()
        marker = "SYNTHETIC_PRIVATE_EXCEPTION_CONTENT"

        def fail(*args: object, **kwargs: object) -> None:
            raise ValueError(marker)

        monkeypatch.setattr("bs4.BeautifulSoup", fail)
        with caplog.at_level(logging.DEBUG, logger=eml_hydrator.__name__):
            result = hydrator.hydrate(
                self._candidate(path, "<html-id@fixture.example.net>"), correlation_id="h2"
            )

        assert isinstance(result, Success)
        assert "milestone" in result.value.canonical_text.lower()
        assert "alert" not in result.value.canonical_text
        assert "BeautifulSoup failed" in caplog.text
        assert marker not in caplog.text

    def test_route_metadata_populated(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "m.eml", _SIMPLE_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(self._candidate(p), correlation_id="hz")
        assert isinstance(result, Success)
        meta = result.value.route_metadata
        assert "Example deployment completed" in meta.get("subject", "")
        assert "sender@fixture.example.net" in meta.get("sender", "")


# ---------------------------------------------------------------------------
# End-to-end: EmlEnumerator → EmlHydrator
# ---------------------------------------------------------------------------


class TestEmlEnumeratorHydratorEndToEnd:
    def test_enumerate_then_hydrate(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "msg.eml", _SIMPLE_EML)

        enum = EmlEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="fixture-user-01@group-01.fixture.example.net",
        )
        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="fixture-user-01@group-01.fixture.example.net",
        )
        enum_result = enum.enumerate(
            RetrievalIntent(entity_type=EntityType.MESSAGE, limit=10),
            correlation_id="e2e-1",
        )
        assert isinstance(enum_result, Success)
        assert len(enum_result.value) == 1
        candidate = enum_result.value[0]
        assert isinstance(candidate, EnumeratedCandidate)

        hydrate_result = hydrator.hydrate(candidate, correlation_id="e2e-1")
        assert isinstance(hydrate_result, Success)
        hydrated = hydrate_result.value
        assert "Example" in hydrated.canonical_text
        assert not hydrated.metadata_only
        assert hydrated.identity.resource_id == "<plain-id@fixture.example.net>"

    @pytest.mark.parametrize("change", ["different", "missing", "duplicate", "duplicate-identical"])
    def test_hydration_refuses_changed_or_ambiguous_message_id(self, tmp_path: Path, change: str) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_eml(inbox / "changed.eml", _SIMPLE_EML)
        enum = EmlEnumerator(inbox_root=inbox, mailbox_tenant_id="t", principal_mailbox="fixture@example.net")
        hydrator = EmlHydrator(mailbox_tenant_id="t", principal_mailbox="fixture@example.net")
        result = enum.enumerate(_make_intent(), correlation_id="identity")
        assert isinstance(result, Success)
        candidate, = result.value
        claimed = Path(candidate.partial_metadata["eml_path"])
        header = "Message-ID: <plain-id@fixture.example.net>\n"
        replacement = {
            "different": "Message-ID: <private-synthetic-changed@fixture.example.net>\n",
            "missing": "",
            "duplicate": header + "Message-ID: <private-synthetic-changed@fixture.example.net>\n",
            "duplicate-identical": header + header,
        }[change]
        _write_eml(claimed, _SIMPLE_EML.replace(header, replacement))

        outcome = hydrator.hydrate(candidate, correlation_id="identity")

        assert isinstance(outcome, Unsupported)
        assert outcome.reason == "eml_identity_invalid"

    @pytest.mark.parametrize(
        "message_id",
        [None, "", "  ", 42, "<private-synthetic-changed@fixture.example.net>"],
        ids=["none", "empty", "blank", "non-string", "different"],
    )
    def test_hydration_refuses_invalid_metadata_identity(self, tmp_path: Path, message_id: object) -> None:
        path = _write_eml(tmp_path / "message.eml", _SIMPLE_EML)
        candidate = _make_candidate(path, "<plain-id@fixture.example.net>")
        candidate = replace(
            candidate,
            partial_metadata={
                **candidate.partial_metadata,
                "message_id": message_id,
            },
        )
        hydrator = EmlHydrator(mailbox_tenant_id="t", principal_mailbox="fixture@example.net")

        outcome = hydrator.hydrate(candidate, correlation_id="identity")

        assert isinstance(outcome, Unsupported)
        assert outcome.reason == "eml_identity_invalid"

    def test_hydration_uses_locator_when_partial_id_is_absent(self, tmp_path: Path) -> None:
        path = _write_eml(tmp_path / "message.eml", _SIMPLE_EML)
        candidate = _make_candidate(path, "<plain-id@fixture.example.net>")
        candidate = replace(candidate, partial_metadata={"eml_path": str(path)})
        hydrator = EmlHydrator(mailbox_tenant_id="t", principal_mailbox="fixture@example.net")

        outcome = hydrator.hydrate(candidate, correlation_id="identity")

        assert isinstance(outcome, Success)
        assert outcome.value.identity.resource_id == "<plain-id@fixture.example.net>"
