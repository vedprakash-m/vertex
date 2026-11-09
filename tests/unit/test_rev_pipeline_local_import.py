"""REV local-import pipeline wiring tests (REV-G2 / REV-G8b / P1-9).

Exercises ``run_rev_cycle`` end-to-end with the local-export Zone C ports
(``EmlEnumerator`` + ``EmlHydrator`` + ``DeterministicRevExtractor``) to verify
the Phase 1/2 wiring added in ``src/core/rev/pipeline.py``:

* 3-dir atomicity file finalization (inbox → claimed → processed / quarantine)
* ``RevCycleReport`` telemetry fields populated from enumerator/hydrator/extractor
  (``claimed_at_startup_count``, ``quarantined_files_count``,
  ``low_unique_body_count``, ``winmail_skipped_count``, ``wall_clock_seconds``)
* ``_rev/last_cycle.json`` atomic checkpoint (schema 1.0)
* ``_rev/cycle_history.jsonl`` bounded (≤10) history
* Crash-recovery replay (files in ``claimed/`` surface first) + idempotent re-run
* Oversized-file quarantine at claim time
* ``grounding_missed.jsonl`` + ``attachment_denied.jsonl`` sidecars

No live M365 consent, no LLM (deterministic extractor).
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.rev.extractor import DeterministicRevExtractor
from src.ai.rev.verification import run_layered_verification
from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits
from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.prompt_shields import LocalOnlyPromptShields
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Incomplete, RateLimited, Success, Unsupported
from src.m365.rev.eml_enumerator import EmlEnumerator
from src.m365.rev.eml_hydrator import EmlHydrator

NOW = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)

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

_EMPTY_BODY_EML = """\
From: sender@example.com
To: tpm@example.com
Subject: Empty
Date: Tue, 24 Jun 2026 15:00:00 +0000
Message-ID: <test-empty@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

"""

_CORRUPT_HISTORY_CASES = (
    b"\xffsynthetic-private-content\n",
    b'{"stop_category":"complete"}\n{"private":"synthetic-private-content"\n',
    b'["synthetic-private-content"]\n',
    b'{"llm_fallback_count":"synthetic-private-content"}\n',
    b'{"llm_fallback_count":[1]}\n',
    b'{"llm_fallback_count":NaN}\n',
    b'{"llm_fallback_count":Infinity}\n',
    b'{"llm_fallback_count":' + b"9" * 5000 + b"}\n",
    b'{"llm_fallback_count":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}\n",
)
_CORRUPT_HISTORY_IDS = (
    "invalid-utf8", "malformed-json", "non-object", "invalid-count", "list-count",
    "nan-count", "infinite-count", "oversize-number", "excessive-nesting",
)

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


def _write_eml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _build_deps(eml_inbox: Path) -> RevPipelineDeps:
    enumerator = EmlEnumerator(
        inbox_root=eml_inbox,
        mailbox_tenant_id="t",
        principal_mailbox="operator@example.com",
        container="inbox",
    )
    hydrator = EmlHydrator(
        mailbox_tenant_id="t",
        principal_mailbox="operator@example.com",
        container="inbox",
        attachment_denied_path=eml_inbox / "attachment_denied.jsonl",
    )
    return RevPipelineDeps(
        enumerator=enumerator,
        hydrator=hydrator,
        shields=LocalOnlyPromptShields(),
        extractor=DeterministicRevExtractor(),
        verifier=lambda **kw: run_layered_verification(**kw).effective_state,
    )


def _run_cycle(
    *,
    deps: RevPipelineDeps,
    programs_root: Path,
    program_id: str = "prog-local",
    correlation_id: str = "test-local",
    set_at: datetime = NOW,
) -> object:
    intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
    return run_rev_cycle(
        program_id=program_id,
        intent=intent,
        deps=deps,
        profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
        mailbox_tenant_id="t",
        mailbox_principal="operator@example.com",
        mailbox_container="inbox",
        correlation_id=correlation_id,
        programs_root=programs_root,
        budget_limits=BudgetLimits(),
        set_at=set_at,
    )


class _UnsupportedEnumerator:
    def enumerate(self, intent, *, correlation_id: str) -> Unsupported:
        return Unsupported(entity_type="private-entity-detail", reason="private-provider-detail")


@pytest.mark.parametrize(
    "replacement",
    [
        "",
        "Date: \n",
        "Date: private-synthetic-invalid-time\n",
        "Date: 2026-06-24T10:00:00\n",
        "Date: Wed, 24 Jun 2026 10:00:00 -0000\n",
        "Date: 2026-06-24T10:00:00-00:00\n",
        "Date: 2026-06-24T10:00:00+12:60\n",
        "Date: 2026-06-24T10:00:00+1260\n",
        "Date: 2026-06-24T10:00:00+12:00:60\n",
        "Date: Wed, 24 Jun 2026 10:00:00 +1260\n",
        "Date: Wed, 24 Jun 2026 10:00:00 +1260 (synthetic comment)\n",
        "Date: Wed, 24 Jun 2026 10:0:00 +1260\n",
        "Date: Wed, 24 Jun 2026 10:00:00 1200\n",
        "Date: Wed, 24 Jun 2026 10:00:00 +1\n",
        "Date: Wed, 24 Jun 2026 10:00:00 1_200\n",
        "Date: Wed, 24 Jun 2026 10:00:00, +1260\n",
        "Date: Wed, 24 Jun 2026 10:00:00, +12\n",
        "Date: Wed, 24 Jun 2026 10:00:00, +1_200\n",
        "Date: Wed, 24 Jun 2026 10:00:00, 1260\n",
        "Date: Wed, 24 Jun 2026 10:00:00,+1260\n",
        "Date: Wed, 24 Jun 2026 1_0:00 +1260\n",
        "Date: Wed, 24 Jun 2026 10:-00 +1260\n",
        "Date: 0001-01-01T00:00:00+14:00\n",
        "Date: Tue, 24 Jun 2026 10:00:00 +0000\nDate: Tue, 24 Jun 2026 11:00:00 +0000\n",
    ],
    ids=[
        "missing", "blank", "malformed", "naive-iso", "unknown-rfc-zone",
        "unknown-iso-zone", "invalid-iso-minute", "invalid-iso-basic-minute",
        "invalid-iso-second", "invalid-rfc-minute", "invalid-rfc-minute-comment",
        "invalid-rfc-minute-short-time", "unsigned-rfc-zone", "short-rfc-zone",
        "noncanonical-rfc-zone", "invalid-rfc-minute-comma", "short-rfc-zone-comma",
        "noncanonical-rfc-zone-comma", "unsigned-rfc-zone-comma",
        "invalid-rfc-minute-attached-comma", "unrecognized-rfc-time-underscore",
        "unrecognized-rfc-time-sign",
        "utc-overflow", "duplicate",
    ],
)
def test_local_mail_missing_or_invalid_source_time_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, replacement: str,
) -> None:
    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    content = _SIMPLE_EML.replace("Date: Tue, 24 Jun 2026 10:00:00 +0000\n", replacement)
    _write_eml(inbox / "invalid-time.eml", content)

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.quarantined == 1
    assert report.candidates_staged == 0
    assert report.assertions_written == 0
    assert report.date_parse_failures == 1
    assert (inbox / "quarantine" / "invalid-time.eml").exists()
    assert not (inbox / "processed" / "invalid-time.eml").exists()
    assert "private-synthetic-invalid-time" not in caplog.text


@pytest.mark.parametrize(
    "replacement",
    ["", "Message-ID: \n"],
    ids=["missing", "blank"],
)
def test_local_mail_without_native_id_is_quarantined(tmp_path: Path, replacement: str) -> None:
    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    content = _SIMPLE_EML.replace("Message-ID: <test-001@example.com>\n", replacement)
    _write_eml(inbox / "missing-id.eml", content)

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.quarantined == 1
    assert report.candidates_staged == 0
    assert report.assertions_written == 0
    assert (inbox / "quarantine" / "missing-id.eml").exists()
    assert not (inbox / "processed" / "missing-id.eml").exists()


@pytest.mark.parametrize(
    "source_date",
    [
        "Tue, 24 Jun 2026 10:00:00 +0000",
        "Wed, 24 Jun 2026 12:00:00 +0200",
        "Wed, 24 Jun 2026 12:0:0 +0200",
        "Wed, 24 Jun 2026 11:59:00 +0159 (synthetic +1260)",
        "Wed, 24 Jun 2026 11:59:00, +0159",
        "Wed, 24 Jun 2026 11.59, +0159",
        "2026-06-24T10:00:00Z",
        "2026-06-24T11:59:00+01:59",
        "2026-06-24T08:30:00-0130",
    ],
    ids=[
        "rfc-utc", "rfc-offset", "rfc-short-time", "rfc-comment", "rfc-comma",
        "rfc-dot-time-comma",
        "iso-utc", "iso-offset", "iso-negative-offset",
    ],
)
def test_local_mail_with_native_id_and_source_time_still_stages(
    tmp_path: Path, source_date: str,
) -> None:
    from src.core.ledger.candidate_store import load_pending_candidates

    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    content = _SIMPLE_EML.replace("Tue, 24 Jun 2026 10:00:00 +0000", source_date)
    _write_eml(inbox / "valid.eml", content)

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.quarantined == 0
    assert report.candidates_staged == 1
    assert report.date_parse_failures == 0
    candidate, = load_pending_candidates("prog-local", programs_root=tmp_path)
    assert candidate.source_ref.message_id == "<test-001@example.com>"
    assert candidate.source_ref.sent_at == NOW
    assert (inbox / "processed" / "valid.eml").exists()


def test_invalid_mail_time_does_not_prevent_independent_valid_mail(tmp_path: Path) -> None:
    from src.core.ledger.candidate_store import load_pending_candidates

    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    _write_eml(
        inbox / "invalid.eml",
        _SIMPLE_EML.replace("Date: Tue, 24 Jun 2026 10:00:00 +0000\n", ""),
    )
    _write_eml(
        inbox / "valid.eml",
        _SIMPLE_EML.replace("<test-001@example.com>", "<test-valid@example.com>"),
    )

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.enumerated == 2
    assert report.quarantined == 1
    assert report.candidates_staged == 1
    assert report.date_parse_failures == 1
    assert report.cycle_integrity_ok
    assert report.extraction_degraded
    candidate, = load_pending_candidates("prog-local", programs_root=tmp_path)
    assert candidate.source_ref.message_id == "<test-valid@example.com>"
    assert (inbox / "quarantine" / "invalid.eml").exists()
    assert (inbox / "processed" / "valid.eml").exists()


def test_rev_run_cli_reports_mail_source_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli
    import src.commands.rev as command
    from src.core.ledger.candidate_store import load_pending_candidates
    from typer.testing import CliRunner

    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    _write_eml(
        inbox / "invalid-time.eml",
        _SIMPLE_EML.replace(
            "Date: Tue, 24 Jun 2026 10:00:00 +0000",
            "Date: private-synthetic-invalid-time",
        ),
    )
    _write_eml(
        inbox / "missing-id.eml",
        _SIMPLE_EML.replace("Message-ID: <test-001@example.com>\n", ""),
    )
    _write_eml(
        inbox / "valid.eml",
        _SIMPLE_EML.replace("<test-001@example.com>", "<test-valid@example.com>"),
    )
    monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)
    for name in (
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: None)
    programs_root = tmp_path / "programs"

    result = CliRunner().invoke(cli.app, [
        "rev", "run", "--program", "prog-local", "--mailbox", "operator@example.com",
        "--eml-inbox", str(inbox), "--programs-root", str(programs_root),
    ])

    assert result.exit_code == 0, repr(result.exception)
    payload, _ = json.JSONDecoder().raw_decode(result.output)
    assert payload["enumerated"] == 3
    assert payload["quarantined"] == 2
    assert payload["date_parse_failures"] == 1
    assert payload["candidates_staged"] == 1
    assert payload["extraction_degraded"] is True
    assert payload["cycle_integrity_ok"] is True
    assert "private-synthetic-invalid-time" not in result.output
    assert (inbox / "quarantine" / "invalid-time.reason.txt").read_text(
        encoding="utf-8"
    ) == "source_time_invalid"
    assert (inbox / "quarantine" / "missing-id.reason.txt").read_text(
        encoding="utf-8"
    ) == "eml_identity_invalid"
    candidate, = load_pending_candidates("prog-local", programs_root=programs_root)
    assert candidate.source_ref.message_id == "<test-valid@example.com>"
    assert (inbox / "processed" / "valid.eml").exists()
    last_cycle = programs_root / "prog-local" / "_rev" / "last_cycle.json"
    assert json.loads(last_cycle.read_text(encoding="utf-8"))["extraction_degraded"] is True


def test_undated_metadata_only_mail_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    content = _EMPTY_BODY_EML.replace("Date: Tue, 24 Jun 2026 15:00:00 +0000\n", "")
    _write_eml(inbox / "undated-empty.eml", content)

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.quarantined == 1
    assert report.date_parse_failures == 1
    assert report.metadata_only == 0
    assert report.candidates_staged == 0
    assert (inbox / "quarantine" / "undated-empty.eml").exists()


def test_hydration_failure_quarantine_omits_private_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    inbox = tmp_path / "synthetic-private-inbox"
    inbox.mkdir()
    _write_eml(inbox / "synthetic-private-name.eml", _SIMPLE_EML)
    deps = _build_deps(inbox)
    marker = "SYNTHETIC_PRIVATE_EXCEPTION_CONTENT"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(marker)

    monkeypatch.setattr(deps.hydrator, "_hydrate_path", fail)
    report = _run_cycle(deps=deps, programs_root=tmp_path)

    assert report.quarantined == 1
    assert report.candidates_staged == 0
    assert (inbox / "quarantine" / "synthetic-private-name.eml").exists()
    reason = (inbox / "quarantine" / "synthetic-private-name.reason.txt").read_text(
        encoding="utf-8"
    )
    assert reason == "hydration_error: source could not be hydrated"
    assert marker not in reason
    assert marker not in caplog.text
    assert str(inbox) not in caplog.text


def test_rev_run_cli_hydration_failure_omits_private_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import cli
    import src.commands.rev as command
    from typer.testing import CliRunner

    inbox = tmp_path / "synthetic-private-inbox"
    inbox.mkdir()
    _write_eml(inbox / "synthetic-private-name.eml", _SIMPLE_EML)
    marker = "SYNTHETIC_PRIVATE_EXCEPTION_CONTENT"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(marker)

    monkeypatch.setattr(EmlHydrator, "_hydrate_path", fail)
    monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)
    for name in (
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: None)
    result = CliRunner().invoke(cli.app, [
        "rev", "run", "--program", "prog-local", "--mailbox", "operator@example.com",
        "--eml-inbox", str(inbox), "--programs-root", str(tmp_path / "programs"),
    ])

    assert result.exit_code == 0, repr(result.exception)
    payload, _ = json.JSONDecoder().raw_decode(result.output)
    assert payload["quarantined"] == 1
    assert payload["candidates_staged"] == 0
    assert (inbox / "quarantine" / "synthetic-private-name.reason.txt").read_text(
        encoding="utf-8"
    ) == "hydration_error: source could not be hydrated"
    assert marker not in result.output
    assert marker not in caplog.text
    assert str(inbox) not in result.output
    assert str(inbox) not in caplog.text


def test_invalid_mail_time_never_reaches_extractor_or_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from src.core.rev import pipeline

    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    _write_eml(
        inbox / "invalid-time.eml",
        _SIMPLE_EML.replace(
            "Date: Tue, 24 Jun 2026 10:00:00 +0000",
            "Date: private-synthetic-invalid-time",
        ),
    )
    deps = _build_deps(inbox)

    def reject_admission(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid source time reached extraction or vaulting")

    monkeypatch.setattr(deps.extractor, "extract", reject_admission)
    monkeypatch.setattr(pipeline, "_vault_evidence", reject_admission)
    report = _run_cycle(deps=deps, programs_root=tmp_path)

    assert report.quarantined == 1
    assert (inbox / "quarantine" / "invalid-time.reason.txt").read_text(
        encoding="utf-8"
    ) == "source_time_invalid"
    assert "private-synthetic-invalid-time" not in caplog.text


def test_invalid_mail_still_consumes_hydration_budget(tmp_path: Path) -> None:
    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    _write_eml(
        inbox / "invalid-time.eml",
        _SIMPLE_EML.replace("Date: Tue, 24 Jun 2026 10:00:00 +0000\n", ""),
    )
    report = run_rev_cycle(
        program_id="prog-local",
        intent=RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25),
        deps=_build_deps(inbox),
        profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
        mailbox_tenant_id="t", mailbox_principal="operator@example.com", mailbox_container="inbox",
        correlation_id="budget-invalid", programs_root=tmp_path,
        budget_limits=BudgetLimits(max_hydrated_bytes_per_cycle=1), set_at=NOW,
    )

    assert report.stop_category == "truncated_by_budget"
    assert report.breached_budget == "max_hydrated_bytes_per_cycle"
    assert report.quarantined == 0
    assert report.date_parse_failures == 0
    assert report.candidates_staged == 0
    assert (inbox / "claimed" / "invalid-time.eml").exists()


@pytest.mark.parametrize("kind", ["quarantined", "date-parse"])
def test_accounted_source_rejection_cannot_run_complete_only_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from src.core.rev import pipeline

    counts = dict(
        enumerated=1,
        hydrated=0,
        quarantined=int(kind == "quarantined"),
        metadata_only=0,
        candidates_staged=0,
        assertions_written=0,
        date_parse_failures=int(kind == "date-parse"),
        gap_transitions=0,
        processed_successfully=0,
        policy_denied=0,
        explicitly_skipped=0,
        terminal_failures=int(kind == "date-parse"),
    )
    calls: list[object] = []
    monkeypatch.setattr(pipeline, "_compute_family_divergence", lambda *args: {})
    monkeypatch.setattr(pipeline, "_run_cross_source_conflict_check", lambda *args: {})
    monkeypatch.setattr(pipeline, "_run_family_flip_gates", lambda *args: calls.append(args) or {})

    report = pipeline._finalize_report(
        program_id="prog-local",
        correlation_id="source-rejection",
        counts=counts,
        stop_reason="",
        stop_category="complete",
        breached="",
        shield_degrade=False,
        wall_seconds=0,
        deps=_build_deps(tmp_path / "rev_inbox"),
        programs_root=tmp_path,
    )

    assert report.cycle_status == "extraction_degraded"
    assert calls == []


@pytest.mark.parametrize("kind", ["source-time", "native-id"])
def test_source_rejection_cannot_run_complete_only_family_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from src.core.rev import pipeline

    inbox = tmp_path / "rev_inbox"
    inbox.mkdir()
    content = _SIMPLE_EML
    if kind == "source-time":
        content = content.replace("Date: Tue, 24 Jun 2026 10:00:00 +0000\n", "")
    else:
        content = content.replace("Message-ID: <test-001@example.com>\n", "")
    _write_eml(inbox / "rejected.eml", content)
    calls: list[object] = []
    monkeypatch.setattr(pipeline, "_run_family_flip_gates", lambda *args: calls.append(args) or {})

    report = _run_cycle(deps=_build_deps(inbox), programs_root=tmp_path)

    assert report.quarantined == 1
    assert calls == []


@pytest.mark.parametrize("source_type", [EntityType.MESSAGE, EntityType.EVENT, EntityType.DRIVE_ITEM])
def test_direct_staging_applies_mail_time_check_only_to_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_type: EntityType,
) -> None:
    from src.ai.rev.extractor import ExtractedClaim
    from src.core.ledger.rev_evidence import EvidenceRef
    from src.core.rev import pipeline
    from src.core.rev.identity import CanonicalItemIdentity

    hydrated = HydratedContent(
        identity=CanonicalItemIdentity(
            source_type=source_type,
            tenant_id="t",
            principal_mailbox="operator@example.com",
            container="inbox",
            resource_id="native-id",
        ),
        canonical_text="The deployment completed.",
        normalized_source_hash="sha256:synthetic",
        chunks=(),
        route_metadata={
            "subject": "Synthetic deployment",
            "sender": "owner@example.com",
            "received_at": "private-synthetic-invalid-time",
        },
    )
    claim = ExtractedClaim(
        event_type="deployment.completed",
        payload={"date": "2026-06-24"},
        evidence_spans=(),
        extraction_confidence=0.9,
        extraction_model="synthetic",
        material=True,
    )
    evidence_ref = EvidenceRef(
        vault_hash="sha256:synthetic-vault",
        representation_version="1",
        start_codepoint=0,
        end_codepoint=10,
        excerpt_hash="sha256:synthetic-excerpt",
        normalized_source_hash="sha256:synthetic",
    )
    fallback_values: list[str] = []

    def non_mail_time(value: str) -> tuple[datetime, bool]:
        fallback_values.append(value)
        return NOW, False

    monkeypatch.setattr(pipeline, "_parse_iso", non_mail_time)
    staged, failures = pipeline._stage_candidates(
        program_id="prog-local", hydrated=hydrated, claims=(claim,),
        evidence_refs=(evidence_ref,), set_at=NOW, programs_root=tmp_path,
    )

    if source_type is EntityType.MESSAGE:
        assert staged == []
        assert failures == 1
        assert fallback_values == []
        assert not (tmp_path / "prog-local" / "ledger").exists()
    else:
        assert len(staged) == 1
        assert failures == 0
        assert fallback_values == ["private-synthetic-invalid-time"]


class TestLocalImportFileFinalization:
    def test_successful_cycle_moves_file_to_processed_and_writes_checkpoint(
        self, tmp_path: Path
    ) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "msg.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.stop_category == "complete"
        assert report.cycle_status == "security_degraded"
        assert report.cycle_integrity_ok is True
        assert report.enumerated == 1
        assert report.processed_successfully == 1
        assert report.explicitly_skipped == 0
        assert report.terminal_failures == 0
        assert report.candidates_staged == 1
        assert report.assertions_written == 1
        # 3-dir atomicity: file moved claimed → processed.
        assert (eml_inbox / "processed" / "msg.eml").exists()
        assert not (eml_inbox / "claimed" / "msg.eml").exists()
        # last_cycle.json atomic checkpoint.
        last_cycle_path = tmp_path / "prog-local" / "_rev" / "last_cycle.json"
        assert last_cycle_path.exists()
        last = json.loads(last_cycle_path.read_text(encoding="utf-8"))
        assert last["schema_version"] == "1.1"
        assert last["stop_category"] == "complete"
        assert last["cycle_status"] == "security_degraded"
        assert last["cycle_integrity_ok"] is True
        assert last["processed_successfully"] == 1
        assert last["candidates_staged"] == 1
        # §6.14.3: extraction_degraded is a persisted boolean field.
        assert last["extraction_degraded"] is False
        # cycle_history.jsonl has one entry.
        hist_path = tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl"
        assert hist_path.exists()
        assert len(hist_path.read_text(encoding="utf-8").splitlines()) == 1

    def test_second_cycle_does_not_reprocess_processed_file(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "msg.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        first = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="c1")
        assert first.candidates_staged == 1
        # Second cycle: file already in processed/ → nothing to enumerate.
        second = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="c2")
        assert second.enumerated == 0
        assert second.candidates_staged == 0
        assert second.stop_category == "complete"
        # cycle_history now has 2 entries.
        hist_path = tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl"
        assert len(hist_path.read_text(encoding="utf-8").splitlines()) == 2

    def test_empty_body_metadata_only_moves_to_processed(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "empty.eml", _EMPTY_BODY_EML)
        deps = _build_deps(eml_inbox)

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.stop_category == "complete"
        assert report.metadata_only == 1
        assert report.candidates_staged == 0
        # Empty-body file finalized to processed/ (handled, not re-hydrated).
        assert (eml_inbox / "processed" / "empty.eml").exists()
        assert not (eml_inbox / "claimed" / "empty.eml").exists()

    def test_oversized_file_quarantined_at_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.m365.rev import eml_enumerator as eml_mod
        # Shrink the size guard so a normal .eml triggers it (fast, no 10 MB write).
        monkeypatch.setattr(eml_mod, "_MAX_EML_BYTES", 50)

        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "big.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.enumerated == 0  # quarantined at claim → no candidate built
        # File moved to quarantine/ with a reason companion.
        assert (eml_inbox / "quarantine" / "big.eml").exists()
        reason_path = eml_inbox / "quarantine" / "big.reason.txt"
        assert reason_path.exists()
        assert "size_exceeded" in reason_path.read_text(encoding="utf-8")
        # Filesystem quarantine count reflected in report telemetry.
        assert report.quarantined_files_count >= 1

    def test_crash_recovery_file_surfaces_first_and_processes(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        deps = _build_deps(eml_inbox)
        # Simulate a prior crash: a file already in claimed/ + a fresh inbox file.
        claimed = deps.enumerator.claimed_dir()
        claimed.mkdir(parents=True, exist_ok=True)
        _write_eml(claimed / "crash.eml", _SIMPLE_EML)
        _write_eml(eml_inbox / "new.eml", _SIMPLE_EML.replace(
            "<test-001@example.com>", "<test-002@example.com>"
        ))

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.enumerated == 2
        assert report.claimed_at_startup_count == 1  # crash.eml was in claimed/ at startup
        # Both files now processed.
        assert (eml_inbox / "processed" / "crash.eml").exists()
        assert (eml_inbox / "processed" / "new.eml").exists()
        assert not any(deps.enumerator.claimed_dir().glob("*.eml"))


class TestLocalImportReportTelemetry:
    def test_report_fields_populated(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "msg.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.wall_clock_seconds >= 0.0
        assert report.winmail_skipped_count == 0  # no ms-tnef in _SIMPLE_EML
        assert report.low_unique_body_count == 0  # no quoted reply
        assert report.llm_fallback_count == 0  # deterministic extractor
        assert report.claimed_at_startup_count == 0
        assert report.quarantined_files_count == 0
        # to_dict surfaces every new field.
        d = report.to_dict()
        for key in (
            "llm_fallback_count", "low_unique_body_count", "winmail_skipped_count",
            "wall_clock_seconds", "quarantined_files_count", "claimed_at_startup_count",
            "date_parse_failures",
            "processed_successfully", "policy_denied", "explicitly_skipped",
            "terminal_failures", "cycle_integrity_ok", "cycle_status",
            "source_unreachable",
        ):
            assert key in d

    def test_budget_stop_accounts_unprocessed_items_as_explicitly_skipped(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "msg.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
        report = run_rev_cycle(
            program_id="prog-local",
            intent=intent,
            deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t",
            mailbox_principal="operator@example.com",
            mailbox_container="inbox",
            correlation_id="budget-stop",
            programs_root=tmp_path,
            budget_limits=BudgetLimits(max_search_requests_total_per_cycle=0),
            set_at=NOW,
        )

        assert report.enumerated == 1
        assert report.processed_successfully == 0
        assert report.explicitly_skipped == 1
        assert report.cycle_integrity_ok is True
        assert report.cycle_status == "extraction_degraded"

    def test_provider_limited_source_sets_source_unreachable_checkpoint(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        deps = _build_deps(eml_inbox)

        class RateLimitedEnumerator:
            def enumerate(self, intent, *, correlation_id: str):
                return RateLimited(provider="eml", retry_after_seconds=30)

        deps = replace(deps, enumerator=RateLimitedEnumerator())

        report = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="source-down")

        assert report.stop_category == "provider_limited"
        assert report.source_unreachable is True
        assert report.cycle_status == "provider_limited"

        last_path = tmp_path / "prog-local" / "_rev" / "last_cycle.json"
        last = json.loads(last_path.read_text(encoding="utf-8"))
        assert last["source_unreachable"] is True

        hist_path = tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl"
        history = [json.loads(line) for line in hist_path.read_text(encoding="utf-8").splitlines()]
        assert history[-1]["source_unreachable"] is True

    def test_unsupported_enumeration_replaces_prior_health_without_finalization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.core.rev.pipeline as pipeline
        from scripts.verify_activation import is_clean_cycle
        from src.core.rev.health import build_rev_health_report, render_rev_health_human

        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        deps = _build_deps(eml_inbox)
        monkeypatch.setattr(pipeline, "_compute_family_divergence", lambda *args: {})
        monkeypatch.setattr(pipeline, "_run_cross_source_conflict_check", lambda *args: {})
        monkeypatch.setattr(pipeline, "_run_family_flip_gates", lambda *args: {})
        prior = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="prior-empty")
        assert prior.stop_category == "complete"
        assert prior.cycle_status == "acquisition_complete"

        def unexpected_finalization(*args: object, **kwargs: object) -> None:
            pytest.fail("Unsupported enumeration must not run fact readers or authority gates")

        monkeypatch.setattr(pipeline, "_compute_family_divergence", unexpected_finalization)
        monkeypatch.setattr(pipeline, "_run_cross_source_conflict_check", unexpected_finalization)
        monkeypatch.setattr(pipeline, "_run_family_flip_gates", unexpected_finalization)
        deps = replace(deps, enumerator=_UnsupportedEnumerator())
        report = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="unsupported")

        assert report.stop_category == "unsupported"
        assert report.stop_reason == "enumeration_unsupported"
        assert report.cycle_status == "unsupported"
        assert report.source_unreachable is False
        assert report.enumerated == report.candidates_staged == 0
        assert report.cycle_integrity_ok is True
        assert "private-provider-detail" not in json.dumps(report.to_dict())
        assert "private-entity-detail" not in json.dumps(report.to_dict())

        rev_dir = tmp_path / "prog-local" / "_rev"
        last = json.loads((rev_dir / "last_cycle.json").read_text(encoding="utf-8"))
        history = [
            json.loads(line)
            for line in (rev_dir / "cycle_history.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert last["correlation_id"] == "unsupported"
        assert last["stop_category"] == last["cycle_status"] == "unsupported"
        assert [entry["stop_category"] for entry in history] == ["complete", "unsupported"]
        assert not is_clean_cycle(last, eml_present=True)
        health = build_rev_health_report("prog-local", programs_root=tmp_path)
        assert health.last_cycle is not None
        assert health.last_cycle.stop_category == "unsupported"
        assert any("enumeration_unsupported" in warning for warning in health.warnings)
        assert any("shield availability was not assessed" in warning for warning in health.warnings)
        assert "stop=unsupported" in render_rev_health_human(health)
        assert "private-provider-detail" not in json.dumps(health.to_dict())

    def test_unsupported_enumeration_cli_is_nonzero_and_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner
        import src.commands.rev as command

        fixture = tmp_path / "messages.json"
        fixture.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(command, "CollectionSearchEnumerator", lambda *args: _UnsupportedEnumerator())
        monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)
        result = CliRunner().invoke(command.app, [
            "run", "--program", "prog-local", "--mailbox", "test@example.com",
            "--mock-fixture", str(fixture), "--programs-root", str(tmp_path / "programs"),
        ])

        assert result.exit_code == 1, result.output
        payload, _ = json.JSONDecoder().raw_decode(result.output)
        assert payload["stop_category"] == payload["cycle_status"] == "unsupported"
        assert payload["stop_reason"] == "enumeration_unsupported"
        assert "private-provider-detail" not in result.output
        assert "private-entity-detail" not in result.output
        last_path = tmp_path / "programs" / "prog-local" / "_rev" / "last_cycle.json"
        assert json.loads(last_path.read_text(encoding="utf-8"))["stop_category"] == "unsupported"

    def test_rev_run_root_cli_keeps_normal_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import cli
        import src.commands.rev as command
        from typer.testing import CliRunner

        calls: list[str] = []
        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, lambda *args, _name=name, **kwargs: calls.append(_name))
        fixture = tmp_path / "messages.json"
        fixture.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(command, "CollectionSearchEnumerator", lambda *args: _UnsupportedEnumerator())
        monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)

        result = CliRunner().invoke(cli.app, [
            "rev", "run", "--program", "prog-local", "--mailbox", "test@example.com",
            "--mock-fixture", str(fixture), "--programs-root", str(tmp_path / "programs"),
        ])

        assert result.exit_code == 1, repr(result.exception)
        assert json.JSONDecoder().raw_decode(result.output)[0]["stop_category"] == "unsupported"
        assert calls == [
            "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ]

    @pytest.mark.parametrize("inbox_option", ["--eml-inbox", "--ics-inbox", "--docs-inbox"])
    def test_unavailable_local_inbox_reports_unsupported_in_actual_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_option: str,
    ) -> None:
        from typer.testing import CliRunner
        import src.commands.rev as command

        inbox = tmp_path / "not-a-directory"
        inbox.write_text("synthetic", encoding="utf-8")
        monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)
        result = CliRunner().invoke(command.app, [
            "run", "--program", "prog-local", "--mailbox", "test@example.com",
            inbox_option, str(inbox), "--programs-root", str(tmp_path / "programs"),
        ])

        assert result.exit_code == 1, result.output
        payload, _ = json.JSONDecoder().raw_decode(result.output)
        assert payload["stop_category"] == payload["cycle_status"] == "unsupported"
        assert payload["stop_reason"] == "enumeration_unsupported"
        assert str(inbox) not in json.dumps(payload)
        assert inbox.read_text(encoding="utf-8") == "synthetic"
        last_path = tmp_path / "programs" / "prog-local" / "_rev" / "last_cycle.json"
        assert json.loads(last_path.read_text(encoding="utf-8"))["stop_category"] == "unsupported"

    @pytest.mark.parametrize("output_format", ["human", "json"])
    @pytest.mark.parametrize(
        "history_content", [None, "missing", *_CORRUPT_HISTORY_CASES],
        ids=["valid", "missing", *_CORRUPT_HISTORY_IDS],
    )
    def test_unsupported_enumeration_is_nonzero_in_doctor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str,
        history_content: bytes | str | None,
    ) -> None:
        import cli
        from src.commands import doctor
        from typer.testing import CliRunner

        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        deps = replace(_build_deps(eml_inbox), enumerator=_UnsupportedEnumerator())
        report = _run_cycle(deps=deps, programs_root=tmp_path)
        assert report.stop_category == "unsupported"
        history = tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl"
        if isinstance(history_content, bytes):
            history.write_bytes(history_content)
        elif history_content == "missing":
            history.unlink()

        for name, path in {
            "PROGRAMS_ROOT": tmp_path, "EDITIONS_ROOT": tmp_path / "editions",
            "REPORTS_ROOT": tmp_path / "reports", "ARCHIVE_ROOT": tmp_path / "archive",
            "REPO_ROOT": tmp_path,
        }.items():
            monkeypatch.setattr(doctor, name, path)
        def unexpected_startup(*args: object, **kwargs: object) -> None:
            raise AssertionError("REV health must not run unrelated root startup")

        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, unexpected_startup)
        result = CliRunner().invoke(cli.app, [
            "--no-catchup", "doctor", "--rev-health", "--rev-program", "prog-local",
            "--format", output_format,
        ])

        assert result.exit_code == 2, result.output
        output = result.output
        if output_format == "json":
            payload = json.loads(output)
            assert payload["last_cycle"]["stop_category"] == "unsupported"
            assert any("shield availability was not assessed" in warning for warning in payload["warnings"])
            assert any("cycle_history_unavailable" in warning for warning in payload["warnings"]) is (history_content is not None)
        else:
            assert "stop=unsupported" in output
            assert "shield availability was not assessed" in output
            assert ("cycle_history_unavailable" in output) is (history_content is not None)
        if isinstance(history_content, bytes):
            assert history.read_bytes() == history_content
            assert not (history.parent / "quarantine").exists()
        elif history_content == "missing":
            assert not history.exists()
        assert "synthetic-private-content" not in output

    def test_rev_health_does_not_compact_an_unselected_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import cli
        from src.commands import doctor
        from typer.testing import CliRunner

        rev_dir = tmp_path / "selected" / "programs" / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        (rev_dir / "last_cycle.json").write_text(json.dumps({
            "stop_category": "unsupported", "enumerated": 0, "shield_degrade": False,
        }), encoding="utf-8")
        (rev_dir / "cycle_history.jsonl").write_text("{}\n", encoding="utf-8")
        for name, folder in {
            "PROGRAMS_ROOT": "programs", "EDITIONS_ROOT": "editions",
            "REPORTS_ROOT": "reports", "ARCHIVE_ROOT": "archive",
        }.items():
            monkeypatch.setattr(doctor, name, tmp_path / "selected" / folder)
        monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path / "selected")
        sentinel = tmp_path / "unselected-compaction"
        monkeypatch.setattr(cli, "_resolve_program_for_session_catchup", lambda argv: "other-program")
        monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", lambda ctx: None)
        monkeypatch.setattr(cli, "_maybe_run_session_catchup", lambda ctx, no_catchup: None)
        monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", lambda program: sentinel.write_text(program))
        result = CliRunner().invoke(cli.app, [
            "doctor", "--edition", "synthetic_weekly", "--rev-health",
            "--rev-program", "prog-local", "--format", "json",
        ])

        assert result.exit_code == 2, repr(result.exception)
        assert not sentinel.exists()
        assert "enumeration_unsupported" in result.output

    @pytest.mark.parametrize(
        "arguments,expected_exit",
        [
            (["--rev-health", "--rev-program"], 2),
            (["--rev-health=invalid"], 2),
            (["--rev-health", "--unknown-option"], 2),
            (["--issue", "not-an-integer", "--rev-health"], 2),
            (["--context=invalid", "--rev-health"], 2),
            (["--rev-health", "--help"], 0),
            (["--help", "--rev-health"], 0),
        ],
    )
    def test_rev_health_argument_errors_do_not_run_root_startup(
        self, monkeypatch: pytest.MonkeyPatch, arguments: list[str], expected_exit: int,
    ) -> None:
        import cli
        from typer.testing import CliRunner

        def unexpected_startup(*args: object, **kwargs: object) -> None:
            raise AssertionError("Invalid REV health invocation ran root startup")

        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, unexpected_startup)
        result = CliRunner().invoke(cli.app, ["doctor", *arguments])
        assert result.exit_code == expected_exit, repr(result.exception)

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--edition", "--rev-health"],
            ["--edition=--rev-health"],
            ["--format=--rev-health"],
            ["--source", "--rev-health"],
            ["--rev-program", "--rev-health"],
        ],
    )
    def test_rev_health_as_an_option_value_keeps_other_doctor_startup(
        self, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
    ) -> None:
        import cli
        from src.commands import doctor
        from typer.testing import CliRunner

        calls: list[str] = []
        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, lambda *args, _name=name, **kwargs: calls.append(_name))
        report = type("RefactorReport", (), {"failures": []})()
        monkeypatch.setattr(doctor, "build_refactor_status_report", lambda **kwargs: report)
        monkeypatch.setattr(doctor, "render_refactor_status_output", lambda *args, **kwargs: "ordinary doctor\n")
        result = CliRunner().invoke(cli.app, ["doctor", *arguments, "--refactor-status"])

        assert result.exit_code == 0, repr(result.exception)
        assert result.output == "ordinary doctor\n"
        assert calls == [
            "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ]

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--edition", "--rev-health", "--issue", "not-an-integer"],
            ["--source", "--rev-health=invalid", "--issue", "not-an-integer"],
            ["--edition", "--rev-health", "--rev-program"],
            ["--context=invalid", "--edition", "--rev-health"],
            ["--refactor-status", "--", "--rev-health"],
        ],
    )
    def test_rev_health_as_an_option_value_with_error_keeps_other_doctor_startup(
        self, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
    ) -> None:
        import cli
        from typer.testing import CliRunner

        calls: list[str] = []
        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, lambda *args, _name=name, **kwargs: calls.append(_name))
        result = CliRunner().invoke(cli.app, ["doctor", *arguments])

        assert result.exit_code == 2, repr(result.exception)
        assert calls == [
            "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ]

    @pytest.mark.parametrize("other_flags", [("--refactor-status",), ("--context", "--fix")])
    def test_rev_health_takes_priority_over_other_doctor_flags_without_startup(
        self, monkeypatch: pytest.MonkeyPatch, other_flags: tuple[str, ...],
    ) -> None:
        import cli
        import typer
        from src.commands import doctor
        from typer.testing import CliRunner

        def unexpected_startup(*args: object, **kwargs: object) -> None:
            raise AssertionError("REV health must not run unrelated root startup")

        for name in (
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ):
            monkeypatch.setattr(cli, name, unexpected_startup)

        def run_health(**kwargs: object) -> None:
            assert kwargs["rev_program"] == "prog-local"
            typer.echo("selected REV health")
            raise typer.Exit(2)

        monkeypatch.setattr(doctor, "_run_rev_health", run_health)
        result = CliRunner().invoke(cli.app, [
            "doctor", *other_flags, "--rev-health", "--rev-program", "prog-local",
        ])

        assert result.exit_code == 2, repr(result.exception)
        assert result.output == "selected REV health\n"

    def test_unsupported_enumeration_health_reports_history_disappearing_during_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.rev.health import build_rev_health_report

        rev_dir = tmp_path / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        history = rev_dir / "cycle_history.jsonl"
        history.write_text('{"llm_fallback_count":1}\n', encoding="utf-8")
        (rev_dir / "last_cycle.json").write_text(json.dumps({
            "stop_category": "unsupported", "enumerated": 0, "shield_degrade": False,
        }), encoding="utf-8")

        original_exists = Path.exists
        original_read_text = Path.read_text
        disappeared = False

        def raced_exists(path: Path) -> bool:
            nonlocal disappeared
            if path == history and not disappeared:
                path.unlink()
                disappeared = True
                return True
            return original_exists(path)

        def raced_read_text(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal disappeared
            if path == history:
                if not disappeared:
                    path.unlink()
                    disappeared = True
                raise FileNotFoundError
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", raced_exists)
        monkeypatch.setattr(Path, "read_text", raced_read_text)
        report = build_rev_health_report("prog-local", programs_root=tmp_path)

        assert disappeared
        assert any("cycle_history_unavailable" in warning for warning in report.warnings)

    @pytest.mark.parametrize(
        "stop_category,shield_degrade,expected_exit",
        [
            ("complete", False, 0),
            ("provider_limited", False, 0),
            ("truncated_by_budget", False, 0),
            ("complete", True, 1),
            ("unsupported", True, 2),
            (None, None, 1),
        ],
    )
    def test_rev_doctor_preserves_other_exit_behavior(
        self, tmp_path: Path, stop_category: str | None, shield_degrade: bool | None,
        expected_exit: int, capsys: pytest.CaptureFixture[str],
    ) -> None:
        import typer
        from src.commands.doctor_checks.context_checks import run_rev_health

        rev_dir = tmp_path / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        if stop_category is not None:
            (rev_dir / "last_cycle.json").write_text(json.dumps({
                "stop_category": stop_category, "shield_degrade": shield_degrade,
            }), encoding="utf-8")
        with pytest.raises(typer.Exit) as exc:
            run_rev_health(
                edition=None, rev_program="prog-local", programs_root=tmp_path,
                editions_root=tmp_path / "editions", format="json",
                resolve_edition_paths_fn=lambda *args: None,
            )
        assert exc.value.exit_code == expected_exit
        warnings = json.loads(capsys.readouterr().out)["warnings"]
        if stop_category == "unsupported":
            assert any("enumeration_unsupported" in warning for warning in warnings)
            assert all("shield availability was not assessed" not in warning for warning in warnings)
        else:
            assert all("enumeration_unsupported" not in warning for warning in warnings)

    @pytest.mark.parametrize("failed_target", ["last_cycle.json", "cycle_history.jsonl"])
    def test_unsupported_enumeration_keeps_failure_visible_if_checkpoint_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        failed_target: str,
    ) -> None:
        import src.core.rev.pipeline as pipeline

        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        deps = replace(_build_deps(eml_inbox), enumerator=_UnsupportedEnumerator())
        rev_dir = tmp_path / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        last_path = rev_dir / "last_cycle.json"
        last_path.write_text('{"stop_category": "complete"}\n', encoding="utf-8")
        history_path = rev_dir / "cycle_history.jsonl"
        history_path.write_text('{"stop_category": "complete"}\n', encoding="utf-8")
        real_replace = pipeline.os.replace

        def fail_checkpoint_replace(src: object, dst: object) -> None:
            if Path(dst).name == failed_target:
                raise OSError("synthetic checkpoint failure")
            real_replace(src, dst)

        monkeypatch.setattr(pipeline.os, "replace", fail_checkpoint_replace)
        report = _run_cycle(deps=deps, programs_root=tmp_path, correlation_id="unsupported")

        assert report.stop_category == "unsupported"
        last = json.loads(last_path.read_text(encoding="utf-8"))
        history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
        assert last["stop_category"] == ("complete" if failed_target == "last_cycle.json" else "unsupported")
        assert history[-1]["stop_category"] == (
            "complete" if failed_target == "cycle_history.jsonl" else "unsupported"
        )
        assert f"could not write {failed_target}" in caplog.text

    @pytest.mark.parametrize("history_content", _CORRUPT_HISTORY_CASES, ids=_CORRUPT_HISTORY_IDS)
    def test_unsupported_enumeration_preserves_invalid_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        history_content: bytes,
    ) -> None:
        from typer.testing import CliRunner
        import src.commands.rev as command

        fixture = tmp_path / "messages.json"
        fixture.write_text("[]", encoding="utf-8")
        programs = tmp_path / "programs"
        rev_dir = programs / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        history = rev_dir / "cycle_history.jsonl"
        history.write_bytes(history_content)
        monkeypatch.setattr(command, "CollectionSearchEnumerator", lambda *args: _UnsupportedEnumerator())
        monkeypatch.setattr(command, "_resolve_shields", LocalOnlyPromptShields)

        result = CliRunner().invoke(command.app, [
            "run", "--program", "prog-local", "--mailbox", "test@example.com",
            "--mock-fixture", str(fixture), "--programs-root", str(programs),
        ])

        assert result.exit_code == 1, result.output
        payload, _ = json.JSONDecoder().raw_decode(result.output)
        assert payload["stop_category"] == payload["cycle_status"] == "unsupported"
        assert history.read_bytes() == history_content
        assert not (rev_dir / "quarantine").exists()
        last = json.loads((rev_dir / "last_cycle.json").read_text(encoding="utf-8"))
        assert last["stop_category"] == "unsupported"
        assert "cycle_history.jsonl" in caplog.text
        assert "synthetic-private-content" not in caplog.text + result.output

    def test_unsupported_enumeration_keeps_valid_history_bounded(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        rev_dir = tmp_path / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        history = rev_dir / "cycle_history.jsonl"
        history.write_text(
            "\n" + "\n".join(json.dumps({
                "correlation_id": f"prior-{i}", "llm_fallback_count": str(i % 3),
            }) for i in range(12)) + "\n",
            encoding="utf-8",
        )

        report = _run_cycle(
            deps=replace(_build_deps(inbox), enumerator=_UnsupportedEnumerator()),
            programs_root=tmp_path, correlation_id="unsupported",
        )

        assert report.stop_category == "unsupported"
        records = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
        assert [record["correlation_id"] for record in records] == [
            *(f"prior-{i}" for i in range(3, 12)), "unsupported",
        ]
        from src.core.rev.health import build_rev_health_report
        health = build_rev_health_report("prog-local", programs_root=tmp_path)
        assert health.llm_fallback_trend == (1, 2, 0)
        assert not (rev_dir / "quarantine").exists()

    def test_unsupported_health_reports_history_read_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.core.rev.health as health

        rev_dir = tmp_path / "prog-local" / "_rev"
        rev_dir.mkdir(parents=True)
        (rev_dir / "last_cycle.json").write_text(json.dumps({
            "stop_category": "unsupported", "enumerated": 0, "shield_degrade": False,
        }), encoding="utf-8")
        (rev_dir / "cycle_history.jsonl").write_text("{}\n", encoding="utf-8")

        def fail_history_read(path: Path) -> list[dict[str, object]]:
            raise OSError("synthetic-private-content")

        monkeypatch.setattr(health, "read_cycle_history_without_repair", fail_history_read)
        report = health.build_rev_health_report("prog-local", programs_root=tmp_path)
        assert any("cycle_history_unavailable" in warning for warning in report.warnings)
        assert "synthetic-private-content" not in json.dumps(report.to_dict())

    def test_attachment_denied_sidecar_written(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "attach.eml", _ATTACHMENT_EML)
        deps = _build_deps(eml_inbox)

        report = _run_cycle(deps=deps, programs_root=tmp_path)

        assert report.enumerated == 1
        denied_log = eml_inbox / "attachment_denied.jsonl"
        assert denied_log.exists()
        records = [json.loads(line) for line in denied_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert records
        assert "application/pdf" in records[0]["denied_content_types"]


class TestCycleHistoryBounding:
    def test_cycle_history_bounded_to_ten(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "msg.eml", _SIMPLE_EML)
        deps = _build_deps(eml_inbox)

        # Run 12 cycles. First processes the file; the rest enumerate 0 but still
        # write a cycle_history entry each. History must be bounded to ≤10.
        for i in range(12):
            _run_cycle(deps=deps, programs_root=tmp_path, correlation_id=f"hist-{i}")

        hist_path = tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl"
        lines = hist_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10  # bounded — oldest 2 dropped
        # Each line is valid JSON with the expected summary keys.
        for line in lines:
            rec = json.loads(line)
            assert "correlation_id" in rec
            assert "stop_category" in rec


class TestHighVolumeBatchedDrain:
    """ADF-W3.2: a truncated enumeration batch must still be processed this
    cycle (not discarded and left for a future crash-recovery pass), and the
    truncation must be classified as ``truncated_by_budget`` (not silently
    misread as ``provider_limited``) so cockpit/doctor telemetry is honest."""

    def _write_n_emls(self, eml_inbox: Path, n: int) -> None:
        for i in range(n):
            _write_eml(
                eml_inbox / f"msg{i:03d}.eml",
                _SIMPLE_EML.replace("<test-001@example.com>", f"<test-{i:03d}@example.com>"),
            )

    @pytest.mark.parametrize("reason,category", [
        ("provider_partial", "provider_limited"),
        ("", "provider_limited"),
        ("page_cut", "truncated_by_budget"),
    ])
    @pytest.mark.parametrize("mode", ["empty", "salvaged", "quiet"])
    def test_incomplete_enumeration_is_never_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        reason: str, category: str, mode: str,
    ) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        if mode != "empty":
            self._write_n_emls(eml_inbox, 1)
        deps = _build_deps(eml_inbox)
        enumerate_original = deps.enumerator.enumerate

        def enumerate_partial(
            intent: RetrievalIntent, *, correlation_id: str,
        ) -> Incomplete[tuple[EnumeratedCandidate, ...]]:
            batch = enumerate_original(intent, correlation_id=correlation_id)
            assert isinstance(batch, Success)
            candidates = batch.value
            if mode == "quiet":
                candidates = tuple(replace(candidate, relevance_score=0.0) for candidate in candidates)
            return Incomplete(candidates, reason=reason)

        monkeypatch.setattr(deps.enumerator, "enumerate", enumerate_partial)
        flip_calls: list[object] = []
        monkeypatch.setattr(
            "src.core.rev.pipeline._run_family_flip_gates",
            lambda *args: flip_calls.append(args) or {},
        )
        report = _run_cycle(deps=deps, programs_root=tmp_path)
        assert report.stop_category == category
        assert report.stop_reason == (reason or "incomplete")
        assert report.processed_successfully == (1 if mode == "salvaged" else 0)
        assert report.explicitly_skipped == (1 if mode == "quiet" else 0)
        assert flip_calls == []
        last = json.loads((tmp_path / "prog-local" / "_rev" / "last_cycle.json").read_text(encoding="utf-8"))
        history = (tmp_path / "prog-local" / "_rev" / "cycle_history.jsonl").read_text(encoding="utf-8")
        for record in (last, json.loads(history.splitlines()[-1])):
            assert record["stop_category"] == category
        assert report.to_dict()["stop_reason"] == (reason or "incomplete")

    def test_incomplete_hydration_retains_provider_limitation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        self._write_n_emls(eml_inbox, 1)
        deps = _build_deps(eml_inbox)
        hydrate_original = deps.hydrator.hydrate

        def hydrate_partial(
            candidate: EnumeratedCandidate, *, correlation_id: str,
        ) -> Incomplete[HydratedContent]:
            hydrated = hydrate_original(candidate, correlation_id=correlation_id)
            assert isinstance(hydrated, Success)
            return Incomplete(hydrated.value, reason="provider_partial")

        monkeypatch.setattr(deps.hydrator, "hydrate", hydrate_partial)
        report = _run_cycle(deps=deps, programs_root=tmp_path)
        assert report.stop_category == "provider_limited"
        assert report.stop_reason == "provider_partial"
        assert report.hydrated == report.processed_successfully == report.candidates_staged == 0
        assert report.explicitly_skipped == 1
        assert len(list(deps.enumerator.claimed_dir().glob("*.eml"))) == 1

    def test_complete_quiet_lane_retains_existing_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        self._write_n_emls(eml_inbox, 1)
        deps = _build_deps(eml_inbox)
        enumerate_original = deps.enumerator.enumerate

        def enumerate_quiet(
            intent: RetrievalIntent, *, correlation_id: str,
        ) -> Success[tuple[EnumeratedCandidate, ...]]:
            batch = enumerate_original(intent, correlation_id=correlation_id)
            assert isinstance(batch, Success)
            return Success(tuple(replace(candidate, relevance_score=0.0) for candidate in batch.value))

        monkeypatch.setattr(deps.enumerator, "enumerate", enumerate_quiet)
        report = _run_cycle(deps=deps, programs_root=tmp_path)
        assert report.stop_category == "complete"
        assert report.stop_reason.startswith("quiet_lane:all_below_threshold")
        assert report.breached_budget == "quiet_lane"
        assert report.explicitly_skipped == 1
        assert report.processed_successfully == 0

    def test_truncated_batch_is_processed_this_cycle_not_discarded(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        self._write_n_emls(eml_inbox, 5)
        deps = _build_deps(eml_inbox)

        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=3)
        report = run_rev_cycle(
            program_id="prog-local", intent=intent, deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t", mailbox_principal="operator@example.com", mailbox_container="inbox",
            correlation_id="vol-1", programs_root=tmp_path,
            budget_limits=BudgetLimits(), set_at=NOW,
        )

        # The truncation is honestly classified as a budget stop...
        assert report.stop_category == "truncated_by_budget"
        # ...but the salvaged batch was still fully processed this cycle, not
        # discarded and left stranded for a future crash-recovery pass.
        assert report.enumerated == 3
        assert report.processed_successfully == 3
        assert report.candidates_staged == 3
        assert len(list((eml_inbox / "processed").glob("*.eml"))) == 3
        # The other 2 files were never claimed — still sitting untouched in inbox/.
        assert len(list(eml_inbox.glob("*.eml"))) == 2
        assert not any(deps.enumerator.claimed_dir().glob("*.eml"))

    def test_multi_cycle_drain_processes_every_file_exactly_once(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        self._write_n_emls(eml_inbox, 5)
        deps = _build_deps(eml_inbox)

        def _cycle(correlation_id: str) -> object:
            intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=3)
            return run_rev_cycle(
                program_id="prog-local", intent=intent, deps=deps,
                profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
                mailbox_tenant_id="t", mailbox_principal="operator@example.com", mailbox_container="inbox",
                correlation_id=correlation_id, programs_root=tmp_path,
                budget_limits=BudgetLimits(), set_at=NOW,
            )

        first = _cycle("vol-c1")
        second = _cycle("vol-c2")

        assert first.stop_category == "truncated_by_budget"
        assert first.processed_successfully == 3
        assert second.stop_category == "complete"
        assert second.processed_successfully == 2
        # All 5 unique messages landed in processed/ exactly once — lossless,
        # no drops, no duplicates across the two-cycle drain.
        processed_names = sorted(p.name for p in (eml_inbox / "processed").glob("*.eml"))
        assert processed_names == [f"msg{i:03d}.eml" for i in range(5)]
        assert not list(eml_inbox.glob("*.eml"))
        assert not any(deps.enumerator.claimed_dir().glob("*.eml"))


class TestCrashLoopGuard:
    """P1-5: a file that survives N consecutive startup recoveries is a poison
    file — quarantine it with reason=crash_loop so it cannot loop forever."""

    def _enumerator(self, inbox: Path) -> EmlEnumerator:
        return EmlEnumerator(
            inbox_root=inbox,
            mailbox_tenant_id="t",
            principal_mailbox="operator@example.com",
            container="inbox",
        )

    def test_file_quarantined_after_three_consecutive_startup_recoveries(
        self, tmp_path: Path
    ) -> None:
        inbox = tmp_path / "rev_inbox"
        claimed = inbox / "claimed"
        claimed.mkdir(parents=True)
        _write_eml(claimed / "poison.eml", _SIMPLE_EML)
        enum = self._enumerator(inbox)
        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)

        # 1st startup recovery → count=1, survivor (simulated crash: no finalize)
        r1 = enum.enumerate(intent, correlation_id="c1")
        assert len(r1.value) == 1
        assert (claimed / "poison.eml").exists()
        # 2nd startup recovery → count=2, survivor
        r2 = enum.enumerate(intent, correlation_id="c2")
        assert len(r2.value) == 1
        assert (claimed / "poison.eml").exists()
        # 3rd startup recovery → count=3 → crash_loop quarantine
        r3 = enum.enumerate(intent, correlation_id="c3")
        assert len(r3.value) == 0  # quarantined → not surfaced
        assert not (claimed / "poison.eml").exists()
        assert (inbox / "quarantine" / "poison.eml").exists()
        reason = (inbox / "quarantine" / "poison.reason.txt").read_text(encoding="utf-8")
        assert "crash_loop" in reason
        # Counter dropped after the loop-quarantine.
        counts = json.loads((inbox / "_crash_loop_counts.json").read_text(encoding="utf-8"))
        assert "poison.eml" not in counts

    def test_successful_processing_resets_crash_loop_counter(self, tmp_path: Path) -> None:
        inbox = tmp_path / "rev_inbox"
        claimed = inbox / "claimed"
        claimed.mkdir(parents=True)
        _write_eml(claimed / "recover.eml", _SIMPLE_EML)
        enum = self._enumerator(inbox)
        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)

        r1 = enum.enumerate(intent, correlation_id="c1")
        eml_path = r1.value[0].partial_metadata["eml_path"]
        # Successful finalization breaks the loop → counter reset.
        enum.mark_processed(eml_path)
        counts = json.loads((inbox / "_crash_loop_counts.json").read_text(encoding="utf-8"))
        assert "recover.eml" not in counts
        # File is now processed, not looping.
        assert (inbox / "processed" / "recover.eml").exists()

    def test_non_loop_quarantine_resets_counter(self, tmp_path: Path) -> None:
        inbox = tmp_path / "rev_inbox"
        claimed = inbox / "claimed"
        claimed.mkdir(parents=True)
        _write_eml(claimed / "bad.eml", _SIMPLE_EML)
        enum = self._enumerator(inbox)
        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
        # First recovery increments the counter to 1.
        enum.enumerate(intent, correlation_id="c1")
        # A non-loop quarantine (e.g. parse error) also breaks the loop.
        enum.mark_quarantined(claimed / "bad.eml", reason="parse_error: simulated")
        counts = json.loads((inbox / "_crash_loop_counts.json").read_text(encoding="utf-8"))
        assert "bad.eml" not in counts
