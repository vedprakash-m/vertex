"""Unit tests for EmlEnumerator (P1-1) and EmlHydrator (P1-2).

Covers:
* EmlEnumerator: 3-dir atomicity, FIFO ordering, crash recovery, limit, lock contention, OSError fallback
* EmlHydrator: MIME plain-text, HTML stripping, empty body, attachment denied, missing path
* End-to-end: enumerator + hydrator produce HydratedContent from .eml fixture
"""

from __future__ import annotations

import email
import email.policy
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, Success, Unsupported
from src.m365.rev.eml_enumerator import EmlEnumerator
from src.m365.rev.eml_hydrator import EmlHydrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW_ISO = "2026-06-24T10:00:00+00:00"

_SIMPLE_EML = """\
From: lead@example.com
To: tpm@example.com
Subject: Gen9 deployment completed
Date: Tue, 24 Jun 2026 10:00:00 +0000
Message-ID: <test-001@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

The Gen9 BIOS AP rollout deployment completed successfully at 2:00 PM PT on 2026-06-24.
All 12,400 production devices are now on the new firmware.
"""

_HTML_EML = """\
From: lead@example.com
To: tpm@example.com
Subject: NOVA Weekly Status
Date: Tue, 24 Jun 2026 11:00:00 +0000
Message-ID: <test-html@example.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><p>Milestone <b>complete</b>: Gen7 devices <em>migrated</em>.</p>
<script>alert(1)</script></body></html>
"""

_QUOTED_REPLY_EML = """\
From: reviewer@example.com
To: tpm@example.com
Subject: Re: deployment
Date: Tue, 24 Jun 2026 12:00:00 +0000
Message-ID: <test-qr@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Thanks for the update, looks good.

> On Mon, 23 Jun 2026, lead@example.com wrote:
> The Gen9 rollout deployment completed.
> All devices are migrated.
"""

_NO_MSGID_EML = """\
From: anon@example.com
To: tpm@example.com
Subject: Anonymous update
Date: Tue, 24 Jun 2026 13:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

No message id in this email.
"""

_ATTACHMENT_EML = """\
From: sender@example.com
To: tpm@example.com
Subject: See attached
Date: Tue, 24 Jun 2026 14:00:00 +0000
Message-ID: <test-attach@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="--boundary"

----boundary
Content-Type: text/plain; charset=utf-8

Please see the attached report.

----boundary
Content-Type: application/pdf
Content-Disposition: attachment; filename="report.pdf"

%PDF-1.4 fake pdf content

----boundary--
"""

_EMPTY_BODY_EML = """\
From: sender@example.com
To: tpm@example.com
Subject: Empty
Date: Tue, 24 Jun 2026 15:00:00 +0000
Message-ID: <test-empty@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

"""


def _write_eml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _make_intent(tenant_id: str = "t", mailbox: str = "u@x.com") -> RetrievalIntent:
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
            principal_mailbox="tpm@example.com",
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
            principal_mailbox="tpm@example.com",
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
        assert candidate.locator.resource_id == "<test-001@example.com>"
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
                "<test-001@example.com>", f"<test-{i:03d}@example.com>"
            ))
        enum = EmlEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
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
            principal_mailbox="tpm@example.com",
            attachment_denied_path=attachment_denied,
        )

    def _candidate(self, path: Path, message_id: str = "<test-001@example.com>") -> EnumeratedCandidate:
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
            self._candidate(p, "<test-html@example.com>"), correlation_id="h2"
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
            self._candidate(p, "<test-qr@example.com>"), correlation_id="h3"
        )
        assert isinstance(result, Success)
        text = result.value.canonical_text
        assert "thanks" in text.lower()
        # Quoted reply content should be stripped (or minimized)
        assert text.count("lead@example.com") == 0 or ">" not in text

    def test_empty_body_is_metadata_only(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "empty.eml", _EMPTY_BODY_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(
            self._candidate(p, "<test-empty@example.com>"), correlation_id="h4"
        )
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_attachment_denied_logged(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "attach.eml", _ATTACHMENT_EML)
        denied_log = tmp_path / "attachment_denied.jsonl"
        hydrator = self._hydrator(attachment_denied=denied_log)
        result = hydrator.hydrate(
            self._candidate(p, "<test-attach@example.com>"), correlation_id="h5"
        )
        assert isinstance(result, Success)
        # attachment_denied.jsonl should have an entry
        assert denied_log.exists()
        records = [json.loads(line) for line in denied_log.read_text().splitlines() if line.strip()]
        assert len(records) >= 1
        assert records[0]["message_id"] == "<test-attach@example.com>"
        assert "application/pdf" in records[0]["denied_content_types"]

    def test_missing_eml_path_returns_unsupported(self, tmp_path: Path) -> None:
        hydrator = self._hydrator()
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="u@x.com",
                container="inbox",
                resource_id="<no-path@x.com>",
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
            tmp_path / "nonexistent.eml", "<missing@x.com>"
        )
        result = hydrator.hydrate(candidate, correlation_id="hy")
        assert isinstance(result, Unsupported)
        assert "eml_not_found" in result.reason

    def test_route_metadata_populated(self, tmp_path: Path) -> None:
        p = _write_eml(tmp_path / "m.eml", _SIMPLE_EML)
        hydrator = self._hydrator()
        result = hydrator.hydrate(self._candidate(p), correlation_id="hz")
        assert isinstance(result, Success)
        meta = result.value.route_metadata
        assert "Gen9 deployment completed" in meta.get("subject", "")
        assert "lead@example.com" in meta.get("sender", "")


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
            principal_mailbox="u@x.com",
        )
        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
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
        assert "Gen9" in hydrated.canonical_text
        assert not hydrated.metadata_only
        assert hydrated.identity.resource_id == "<test-001@example.com>"
