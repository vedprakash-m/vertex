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
from src.core.rev.prompt_shields import LocalOnlyPromptShields
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import RateLimited
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
        principal_mailbox="u@x.com",
        container="inbox",
    )
    hydrator = EmlHydrator(
        mailbox_tenant_id="t",
        principal_mailbox="u@x.com",
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
        mailbox_principal="u@x.com",
        mailbox_container="inbox",
        correlation_id=correlation_id,
        programs_root=programs_root,
        budget_limits=BudgetLimits(),
        set_at=set_at,
    )


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
            mailbox_principal="u@x.com",
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

    def test_truncated_batch_is_processed_this_cycle_not_discarded(self, tmp_path: Path) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        self._write_n_emls(eml_inbox, 5)
        deps = _build_deps(eml_inbox)

        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=3)
        report = run_rev_cycle(
            program_id="prog-local", intent=intent, deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t", mailbox_principal="u@x.com", mailbox_container="inbox",
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
                mailbox_tenant_id="t", mailbox_principal="u@x.com", mailbox_container="inbox",
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
            principal_mailbox="u@x.com",
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
