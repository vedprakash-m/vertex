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
    return Program(schema_version="3.0", id="xpf", name="XPF")


def _signal(signal_id: str) -> Signal:
    from datetime import datetime, timezone

    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 7, 13, tzinfo=timezone.utc),
        source="workiq",
        program_id="xpf",
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
    monkeypatch.setattr(prefetch_module, "list_editions_for_program", lambda program_id, programs_root=None: ("xpf_weekly",))
    monkeypatch.setattr(prefetch_module, "load_current_workstreams", lambda program_id, programs_root=None: ())
    monkeypatch.setattr(prefetch_module, "load_latest_confirmed_snapshot_items", lambda edition_name, archive_root=None: None)
    return programs_root


def test_prefetch_commits_a_snapshot_on_success(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: (_signal("s1"), _signal("s2")))

    result = _invoke(["--program", "xpf"])
    assert result.exit_code == 0, result.output
    assert "signals=2" in result.output
    assert "completeness=complete" in result.output

    manifest = read_unexpired_committed_snapshot("xpf", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "complete"


def test_prefetch_degrades_on_live_bridge_failure(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kwargs):
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(gather_module, "_build_workiq_signals", _raise)

    result = _invoke(["--program", "xpf"])
    assert result.exit_code == 0, result.output
    assert "degraded" in result.output.lower()

    manifest = read_unexpired_committed_snapshot("xpf", "workiq", programs_root=_isolate)
    assert manifest is not None
    assert manifest.completeness == "degraded"


def test_prefetch_rejects_unsupported_channel(_isolate: Path) -> None:
    result = _invoke(["--program", "xpf", "--channel", "kusto"])
    assert result.exit_code != 0


def test_prefetch_exits_when_program_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefetch_module, "load_program", lambda program_id, programs_root=None: None)
    result = _invoke(["--program", "nope"])
    assert result.exit_code == 1


def test_prefetch_fails_cleanly_when_lease_busy(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_module, "_build_workiq_signals", lambda **kwargs: ())
    # Hold the lease from "another owner" before invoking the command.
    acquire_lease("xpf", "someone_else", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=_isolate)

    result = _invoke(["--program", "xpf"])
    assert result.exit_code == 1
    assert "busy" in result.output.lower()
