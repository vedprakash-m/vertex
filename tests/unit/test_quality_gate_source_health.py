"""Direct coverage for the extracted source-health gate (QG-SG-01).

Guards the D-09 / Phase 3 peel of the source-health cluster from the
``src/core/quality_gates`` package into
``src/core/quality_gates/source_health.py`` (re-exported from ``__init__``).
The two ``source_health`` summary builders are monkeypatched in the submodule
namespace so the gate's branching can be exercised in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.core.quality_gates import evaluate_source_health_gates
from src.core.quality_gates import source_health


def _call(**overrides):
    kwargs = dict(
        program_id="acme",
        edition_name="acme_weekly",
        slice_contracts=(object(),),
        gather_state=SimpleNamespace(),
        waivers=(),
        function_name="newsletter",
    )
    kwargs.update(overrides)
    return evaluate_source_health_gates(**kwargs)


def test_no_program_is_noop() -> None:
    assert _call(program_id=None).results == ()


def test_no_gather_state_no_contracts_is_noop() -> None:
    assert _call(gather_state=None, slice_contracts=None).results == ()


def test_no_gather_state_with_contracts_blocks() -> None:
    report = _call(gather_state=None)
    gate = report.results[0]
    assert gate.gate_id == "QG-SG-01" and gate.passed is False and gate.exit_code == 1
    assert "no gather state recorded" in gate.message


def test_no_contracts_transcript_healthy_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    assert _call(slice_contracts=None).results == ()


def test_no_contracts_transcript_blocks(monkeypatch) -> None:
    transcript = SimpleNamespace(blocks_confirm=True, state="zero_yield", waiver=None)
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: transcript)
    report = _call(slice_contracts=None)
    gate = report.results[0]
    assert gate.passed is False and gate.exit_code == 1
    assert gate.forceable is True  # zero_yield is forceable
    assert "Transcript channel is configured but unhealthy" in gate.message


def test_summary_none_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    monkeypatch.setattr(source_health, "build_slice_source_health_summary", lambda *a, **k: None)
    assert _call().results == ()


def _summary(unhealthy_roles=(), contract_count=2, function="newsletter"):
    return SimpleNamespace(unhealthy_roles=tuple(unhealthy_roles), contract_count=contract_count, function=function)


def test_summary_no_blocking_roles_passes(monkeypatch) -> None:
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    monkeypatch.setattr(source_health, "build_slice_source_health_summary", lambda *a, **k: _summary())
    report = _call()
    gate = report.results[0]
    assert gate.passed is True and gate.forceable is True
    assert "source health gate passed" in gate.message.lower()


def test_summary_blocking_unbound_role_is_non_forceable(monkeypatch) -> None:
    role = SimpleNamespace(blocks_confirm=True, waiver=None, state="unbound", contract_id="c1", role="kusto")
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    monkeypatch.setattr(source_health, "build_slice_source_health_summary", lambda *a, **k: _summary([role]))
    report = _call()
    gate = report.results[0]
    assert gate.passed is False and gate.forceable is False
    assert "Fix the slice/source binding" in gate.message


def test_summary_blocking_bound_role_is_forceable(monkeypatch) -> None:
    role = SimpleNamespace(blocks_confirm=True, waiver=None, state="zero_yield", contract_id="c1", role="kusto")
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    monkeypatch.setattr(source_health, "build_slice_source_health_summary", lambda *a, **k: _summary([role]))
    report = _call()
    gate = report.results[0]
    assert gate.passed is False and gate.forceable is True
    assert "vertex gather" in gate.message


def test_summary_waived_roles_reported_in_pass_message(monkeypatch) -> None:
    waived = SimpleNamespace(blocks_confirm=False, waiver=object(), state="zero_yield", contract_id="c1", role="kusto")
    monkeypatch.setattr(source_health, "build_transcript_source_health", lambda *a, **k: None)
    monkeypatch.setattr(source_health, "build_slice_source_health_summary", lambda *a, **k: _summary([waived]))
    report = _call()
    gate = report.results[0]
    assert gate.passed is True
    assert "active waiver(s)" in gate.message
