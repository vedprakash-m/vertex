"""ADF-W1.5/ADF-W1.10 remainder: `vertex prefetch` CLI (src/commands/prefetch.py)."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.gather as gather_module
import src.commands.prefetch as prefetch_module
from cli import app
from src.core.models import Confidence
from src.core.models_v2 import Program, Signal
from src.core.prefetch_store import read_unexpired_committed_snapshot
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, acquire_lease

runner = CliRunner()


def _invoke(args: list[str]):
    return runner.invoke(app, ["prefetch", *args])


def _program() -> Program:
    return Program(schema_version="3.0", id="samplealpha", name="SAMPLEALPHA")


def _signal(signal_id: str) -> Signal:
    from datetime import datetime, timezone

    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 7, 13, tzinfo=timezone.utc),
        source="workiq",
        program_id="samplealpha",
        workstream_id=None,
        entity_refs=(),
        text="text",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(prefetch_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(prefetch_module, "load_program", lambda program_id, programs_root=None: _program())
    monkeypatch.setattr(prefetch_module, "list_editions_for_program", lambda program_id, programs_root=None: ("samplealpha_weekly",))
    monkeypatch.setattr(prefetch_module, "load_current_workstreams", lambda program_id, programs_root=None: ())
    monkeypatch.setattr(prefetch_module, "load_latest_confirmed_snapshot_items", lambda edition_name, archive_root=None: None)
    return programs_root


def test_prefetch_commits_a_snapshot_on_success(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: (_signal("s1"), _signal("s2")))

    result = _invoke(["--program", "samplealpha"])
    assert result.exit_code == 0, result.output
    assert "signals=2" in result.output
    assert "completeness=complete" in result.output

    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "complete"


def test_prefetch_degrades_on_live_bridge_failure(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kwargs):
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(gather_module, "_build_workiq_signals", _raise)

    result = _invoke(["--program", "samplealpha"])
    assert result.exit_code == 0, result.output
    assert "degraded" in result.output.lower()

    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "degraded"


def test_prefetch_rejects_unsupported_channel(_isolate: Path) -> None:
    result = _invoke(["--program", "samplealpha", "--channel", "kusto"])
    assert result.exit_code != 0


def test_prefetch_exits_when_program_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefetch_module, "load_program", lambda program_id, programs_root=None: None)
    result = _invoke(["--program", "nope"])
    assert result.exit_code == 1


def test_prefetch_fails_cleanly_when_lease_busy(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: ())
    # Hold the lease from "another owner" before invoking the command.
    acquire_lease("samplealpha", "someone_else", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=_isolate)

    result = _invoke(["--program", "samplealpha"])
    assert result.exit_code == 1
    assert "busy" in result.output.lower()


@pytest.mark.parametrize("include_valid", [False, True])
def test_prefetch_marks_quarantined_workiq_snapshots_degraded(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch, include_valid: bool,
) -> None:
    from datetime import datetime, timezone
    from src.commands.gather_pipeline.workiq_prefetch_stage import resolve_workiq_signals
    from src.core.models_v2 import IntegrationError

    as_of = datetime(2026, 7, 13, tzinfo=timezone.utc)
    diagnostic = gather_module._signals_from_workiq_payload(
        payload={"emails": [{"id": "private-id", "receivedDateTime": "invalid"}]},
        query_name="synthetic", question="Find status", program_id="samplealpha", as_of=as_of,
        items_by_id={}, workstreams=(),
    )[0]
    signals = (diagnostic, _signal("valid")) if include_valid else (diagnostic,)
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: signals)

    result = _invoke(["--program", "samplealpha"])

    assert result.exit_code == 0, result.output
    assert "completeness=degraded" in result.output
    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "degraded"
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=_isolate,
        live_fetch_fn=lambda: pytest.fail("The committed snapshot should be used"),
        integration_error_sink=errors,
    )
    assert len(restored) == len(signals)
    assert len(errors) == 1 and errors[0].stage == "quarantine"


@pytest.mark.parametrize("include_valid", [False, True])
def test_prefetch_marks_a_supplied_workiq_budget_diagnostic_degraded(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch, include_valid: bool,
) -> None:
    from dataclasses import replace

    from src.commands.gather_pipeline.workiq_prefetch_stage import resolve_workiq_signals
    from src.core.models_v2 import IntegrationError

    diagnostic = replace(
        _signal("budget"), source="system", confidence=Confidence.NONE,
        metadata={
            "integration_source": "workiq", "workiq_plan_budget_exhausted": True,
            "error": "WorkIQ query-plan budget exhausted; at least one plan was not attempted.",
        },
    )
    signals = (_signal("valid"), diagnostic) if include_valid else (diagnostic,)
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: signals)

    result = _invoke(["--program", "samplealpha"])

    assert result.exit_code == 0, result.output
    assert "completeness=degraded" in result.output
    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None and manifest.completeness == "degraded"
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=_isolate,
        live_fetch_fn=lambda: pytest.fail("The committed snapshot should be used"),
        integration_error_sink=errors,
    )
    assert len(restored) == len(signals)
    assert len(errors) == 1 and errors[0].stage == "budget" and errors[0].retryable is True


@pytest.mark.parametrize(
    ("kind", "include_valid"),
    [("time", False), ("time", True), ("identity_missing", False), ("identity_missing", True),
     ("invalid_shape", False), ("invalid_shape", True), ("unsafe_control", False), ("unsafe_control", True),
     ("unsafe_url", False), ("unsafe_url", True),
     ("unavailable", False), ("malformed", False)],
)
def test_prefetch_replays_structured_gather_diagnostics(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch, kind: str, include_valid: bool,
) -> None:
    from dataclasses import replace
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from src.commands.gather_pipeline.workiq_prefetch_stage import resolve_workiq_signals
    from src.core.models_v2 import (
        IntegrationError, M365Config, WorkIQRetrievalConfig, Workstream, WorkstreamSignalSources,
    )
    from src.m365.agency_bridge import AgencyBridge

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 24, tzinfo=timezone.utc)

    program = replace(_program(), m365=M365Config(
        enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Find status"},
        retrieval=WorkIQRetrievalConfig(discovery_mode="structured_json"),
    ))
    workstream = Workstream(
        id="synthetic", name="Synthetic lane",
        signal_sources=WorkstreamSignalSources(workiq_keywords=("synthetic",)),
    )
    records: list[object] = [{"id": "private-missing", "subject": "Private invalid subject"}]
    if kind == "identity_missing":
        records = [{"subject": "Private invalid subject", "receivedDateTime": "2026-10-24T12:00:00Z"}]
    elif kind == "invalid_shape":
        records = ["Private malformed record"]
    elif kind == "unsafe_control":
        records = [{"id": "private-control", "subject": "Private \x1b[2J content", "receivedDateTime": "2026-10-24T12:00:00Z"}]
    elif kind == "unsafe_url":
        records = [{"id": "private-url", "webUrl": "https://untrusted.invalid/private", "receivedDateTime": "2026-10-24T12:00:00Z"}]
    if include_valid:
        records.append({
            "conversationId": "turn1search2", "id": "valid",
            "subject": "Synthetic valid", "receivedDateTime": "2026-10-24T12:00:00Z",
        })
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True
    bridge.probe.return_value.has_workiq_cli = False
    bridge.probe.return_value.server_tools = {}
    bridge.last_mcp_error.return_value = None

    def ask_workiq(question, **kwargs):
        if "Which of my emails received between" in question:
            if kind == "unavailable":
                return None
            if kind == "malformed":
                return {"response": "Private malformed response"}
            return {"emails": records}
        if "calendar and meetings" in question:
            return {"events": []}
        if "email messages or conversation threads" in question:
            return {"emails": []}
        if "Microsoft Teams messages" in question:
            return {"messages": []}
        pytest.fail("Unexpected synthetic WorkIQ question")

    bridge.ask_workiq.side_effect = ask_workiq
    monkeypatch.setattr(prefetch_module, "datetime", Clock)
    monkeypatch.setattr(prefetch_module, "AgencyBridge", lambda: bridge)
    monkeypatch.setattr(prefetch_module, "load_program", lambda *args, **kwargs: program)
    monkeypatch.setattr(prefetch_module, "load_current_workstreams", lambda *args, **kwargs: (workstream,))

    result = _invoke(["--program", "samplealpha"])

    assert result.exit_code == 0, result.output
    assert "completeness=degraded" in result.output
    assert "completed with an error" not in result.output, result.output
    questions = [call.args[0] for call in bridge.ask_workiq.call_args_list]
    assert sum("Which of my emails received between" in question for question in questions) == 1
    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None and manifest.completeness == "degraded"
    errors: list[IntegrationError] = []
    signals = resolve_workiq_signals(
        program_id="samplealpha", programs_root=_isolate,
        live_fetch_fn=lambda: pytest.fail("The committed snapshot should be used"),
        integration_error_sink=errors,
    )
    assert len(signals) == 1 + int(include_valid)
    quarantine_kinds = {"time", "identity_missing", "invalid_shape", "unsafe_control", "unsafe_url"}
    expected_stage = "quarantine" if kind in quarantine_kinds else "discovery"
    assert len(errors) == 1 and errors[0].stage == expected_stage
    assert errors[0].retryable is (kind not in quarantine_kinds)
    if kind in {"invalid_shape", "unsafe_control", "unsafe_url"}:
        assert "malformed or unsafe" in errors[0].message
        assert errors[0].operator_action == "Check record format, remove terminal controls and verify permitted mail URLs before retrying."
    assert "private-missing" not in repr(signals) + repr(errors) + result.output
    assert "Private invalid subject" not in repr(signals) + repr(errors) + result.output
    assert "Private malformed response" not in repr(signals) + repr(errors) + result.output
    for private_value in ("Private", "\\x1b[2J", "private-control", "private-url", "untrusted.invalid"):
        assert private_value not in repr(signals) + repr(errors) + result.output


@pytest.mark.parametrize(
    ("source", "metadata"),
    [
        ("system", {"integration_source": "other", "quarantined": True}),
        ("system", {"integration_source": "workiq"}),
        ("workiq", {"integration_source": "workiq", "quarantined": True}),
        ("system", {"integration_source": "other", "workiq_structured_response_unusable": True}),
        ("system", {"integration_source": "workiq", "workiq_structured_response_unusable": False}),
        ("system", {"integration_source": "other", "workiq_plan_budget_exhausted": True}),
        ("system", {"integration_source": "workiq", "workiq_plan_budget_exhausted": False}),
        ("workiq/email", {"integration_source": "workiq", "workiq_plan_budget_exhausted": True}),
    ],
)
def test_prefetch_does_not_degrade_for_non_quarantine_signals(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch, source: str, metadata: dict[str, object],
) -> None:
    from dataclasses import replace

    signal = replace(_signal("ordinary"), source=source, metadata=metadata)
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: (signal,))
    result = _invoke(["--program", "samplealpha"])
    assert result.exit_code == 0, result.output
    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None and manifest.completeness == "complete"


def test_prefetch_empty_result_remains_partial(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: ())
    result = _invoke(["--program", "samplealpha"])
    assert result.exit_code == 0, result.output
    assert "completeness=partial" in result.output
    manifest = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "partial"


def test_repeated_prefetch_preserves_committed_payload_and_remains_usable(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands.gather_pipeline.workiq_prefetch_stage import resolve_workiq_signals

    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: (_signal("same"),))
    first_result = _invoke(["--program", "samplealpha"])
    assert first_result.exit_code == 0, first_result.output
    first = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert first is not None
    payload_path = _isolate / "samplealpha" / "runtime" / "prefetch" / "workiq" / first.snapshot_id / "payload.json"
    payload_before = payload_path.read_bytes()
    original_open = Path.open

    def interrupt_in_place_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path == payload_path and mode == "wb":
            with original_open(path, mode, *args, **kwargs) as handle:
                handle.write(b'{"partial":')
            raise OSError("Synthetic interrupted in-place write")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupt_in_place_open)
    second_result = _invoke(["--program", "samplealpha"])
    assert second_result.exit_code == 0, second_result.output
    assert payload_path.read_bytes() == payload_before
    second = read_unexpired_committed_snapshot("samplealpha", "workiq", programs_root=_isolate)
    assert second is not None and second.snapshot_id == first.snapshot_id
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=_isolate,
        live_fetch_fn=lambda: pytest.fail("The committed snapshot should remain usable"),
    )
    assert [signal.id for signal in restored] == ["same"]
