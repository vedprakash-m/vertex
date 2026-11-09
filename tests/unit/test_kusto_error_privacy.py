from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.core.decision_register import load_decisions
from src.core.exceptions import AuthError, CredentialExpired, QueryError, QueryTimeoutError
from src.core.failure_taxonomy import _PATTERN_TABLE, FailureCategory, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.journal import append_signal, read_signals
from src.core.knowledge_store import KnowledgeStore
from src.core.models import Confidence
from src.core.models_v2 import ADOConfig, KustoConfig, KustoQuery, Program, Workstream
from src.core.program_fact_store import load_program_facts
from src.core.signal_dedup import build_deterministic_signal_id


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PROGRAM = "synthetic-kusto-program"
_PRIVATE = "SYNTHETICPRIVATEPROVIDERDETAIL"
_CONNECTOR = "SYNTHETICPRIVATECONNECTOR"
_AUTH_METHOD = "SYNTHETICPRIVATEAUTHMETHOD"


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    program = Program(
        schema_version="2.0",
        id=_PROGRAM,
        name="Synthetic",
        ado=ADOConfig(
            organization="example",
            project="Example",
            area_paths=("Example\\Area",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (Workstream(id="ws", name="Example", area_paths=("Example\\Area",)),)
    query = KustoQuery(
        id="synthetic-query",
        cluster="https://example.test",
        database="Example",
        kql="Synthetic | take 1",
        section="Synthetic",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM,),
        validated=True,
    )
    calls: list[str] = []
    behavior = SimpleNamespace(error=QueryError(f"429 {_PRIVATE}"), fail=True, route="legacy")

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        calls.append(query.id)
        if behavior.fail:
            raise behavior.error
        return [{"Value": 1}]

    def resolve_binding(_program: object, _workstreams: object, channel: str, **_kwargs: object) -> object | None:
        return object() if channel == "kusto" and behavior.route == "uil" else None

    def uil(*_args: object, **_kwargs: object) -> tuple[tuple[()], int]:
        calls.append("uil")
        if behavior.fail:
            raise behavior.error
        return (), 0

    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda *_args, **_kwargs: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=(query,)
        ),
    )
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", resolve_binding)
    monkeypatch.setattr(gather, "_load_kusto_signals_via_uil", uil)
    return SimpleNamespace(root=root, calls=calls, behavior=behavior, execute=execute)


def _gather(scenario: SimpleNamespace, *, as_of: datetime = _NOW) -> Any:
    return gather.gather_program(
        _PROGRAM,
        programs_root=scenario.root,
        as_of=as_of,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_kusto=True,
        kusto_query_executor=scenario.execute,
    )


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("route", ["legacy", "uil"])
@pytest.mark.parametrize(
    "error_type", [AuthError, CredentialExpired, QueryError, QueryTimeoutError, TimeoutError, typer.BadParameter]
)
def test_actual_gather_surfaces_omit_provider_detail(
    scenario: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    gather_v2: bool,
    route: str,
    error_type: type[Exception],
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    scenario.behavior.route = route
    scenario.behavior.error = error_type(f"429 {_PROGRAM} {_PRIVATE}")
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    assert state is not None and manifest is not None
    assert scenario.calls == (["synthetic-query"] if route == "legacy" else ["uil"])
    assert artifacts.integration_error_count == 1
    assert (artifacts.discovered_signals, artifacts.new_signals, artifacts.pending_review) == (1, 1, 1)
    assert gather._semantic_gather_exit_code(artifacts) == 2
    error = artifacts.integration_errors[0]
    assert (error.source, error.stage, error.retryable) == ("kusto", "gather", True)
    assert error.operator_action
    assert state.integration_error_details == (error,)
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "kusto")
    assert outcome.degraded and outcome.degrade_reason == error.message
    assert state.channels["kusto"]["last_error"] == error.message
    expected = classify_exception(str(scenario.behavior.error))
    assert classify_exception(error.message) == expected
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )
    diagnostic = next(signal for signal in signals if (signal.metadata or {}).get("integration_source") == "kusto")
    assert diagnostic.source == "system" and diagnostic.confidence == Confidence.NONE
    assert (diagnostic.metadata or {}).get("signal_class") == "status"
    warnings = [record for record in caplog.records if record.getMessage().startswith("Kusto gather failed")]
    assert len(warnings) == 1 and warnings[0].args == () and warnings[0].exc_info is None
    stderr = capsys.readouterr().err
    assert ("[ACTION REQUIRED]" in stderr) is (error_type is CredentialExpired)
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details) + repr(state.channels),
        "manifest": repr(manifest.channel_outcomes) + repr(manifest.failed_refs),
        "signals": repr(signals),
        "observability": repr(report),
        "logs": caplog.text,
        "stderr": stderr,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert _PROGRAM not in error.message and _PROGRAM not in warnings[0].getMessage()


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_credential_expiry_banner_omits_provider_fields(
    scenario: SimpleNamespace, capsys: pytest.CaptureFixture[str], route: str,
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = CredentialExpired(_PRIVATE, connector=_CONNECTOR, auth_method=_AUTH_METHOD)
    artifacts = _gather(scenario)
    stderr = capsys.readouterr().err
    assert artifacts.integration_error_count == 1
    assert "[ACTION REQUIRED]" in stderr and "vertex doctor --adapter-cert" in stderr
    assert all(value not in stderr for value in (_PRIVATE, _CONNECTOR, _AUTH_METHOD))


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_kpi_failure_keeps_outer_failure_semantics_and_omits_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    scenario.behavior.route = route
    scenario.behavior.fail = False

    def fail_kpi(*_args: object, **_kwargs: object) -> None:
        raise QueryError(f"429 {_PRIVATE}")

    monkeypatch.setattr(gather, "_build_kusto_kpi_signals", fail_kpi)
    artifacts = _gather(scenario)
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert scenario.calls == (["synthetic-query"] if route == "legacy" else ["uil"])
    assert artifacts.integration_error_count == 1 and gather._semantic_gather_exit_code(artifacts) == 2
    assert len(signals) == 1 and signals[0].source == "system"
    assert _PRIVATE not in repr(artifacts.integration_errors) and _PRIVATE not in repr(signals)


@pytest.mark.parametrize("route", ["legacy", "uil"])
@pytest.mark.parametrize("text", ["403 blocked by", "risk"])
def test_failure_signal_class_comes_from_safe_diagnostic(
    scenario: SimpleNamespace, route: str, text: str,
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = QueryError(f"{text} {_PRIVATE}")
    _gather(scenario)
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert len(signals) == 1 and (signals[0].metadata or {}).get("signal_class") == "status"


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_provider_failure_does_not_create_an_action(
    scenario: SimpleNamespace, route: str,
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = QueryError(f"action: {_PRIVATE}")
    _gather(scenario)
    snapshot = load_program_facts(_PROGRAM, programs_root=scenario.root, fact_types=("action.item",))
    assert snapshot.facts == ()


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_provider_failure_does_not_create_a_decision(
    scenario: SimpleNamespace, route: str,
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = QueryError(f"decision: {_PRIVATE}")
    _gather(scenario)
    assert load_decisions(_PROGRAM, programs_root=scenario.root) == ()


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("429 too many requests", FailureCategory.RATE_LIMIT),
        ("401 unauthorized", FailureCategory.TRANSIENT_AUTH),
        ("403 forbidden", FailureCategory.PERMISSION),
        ("query timeout", FailureCategory.QUERY_TIMEOUT),
        ("connection refused", FailureCategory.NETWORK),
        ("disk full", FailureCategory.RESOURCE),
        ("schema error", FailureCategory.SCHEMA_DRIFT),
        ("invalid manifest", FailureCategory.DATA_CORRUPTION),
        ("redaction failure", FailureCategory.PII_LEAK),
        ("invalid config", FailureCategory.CONFIG),
        ("novel failure", FailureCategory.UNKNOWN),
        ("429 unauthorized", FailureCategory.RATE_LIMIT),
    ],
)
def test_observability_preserves_classification_without_provider_text(
    scenario: SimpleNamespace, text: str, category: FailureCategory,
) -> None:
    scenario.behavior.error = QueryError(f"{text} {_PRIVATE}")
    expected = classify_exception(str(scenario.behavior.error))
    artifacts = _gather(scenario)
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    assert artifacts.integration_error_count == 1 and expected.category is category
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )
    assert _PRIVATE not in repr(artifacts.integration_errors) and _PRIVATE not in repr(report)


def test_taxonomy_pattern_details_reclassify_consistently() -> None:
    for _, patterns, _ in _PATTERN_TABLE:
        for pattern in patterns:
            original = classify_exception(f"{pattern} {_PRIVATE}")
            assert _PRIVATE not in original.detail
            assert classify_exception(f"Kusto gather failed; {original.detail}") == original


def test_same_time_failures_deduplicate_by_safe_taxonomy_match(scenario: SimpleNamespace) -> None:
    scenario.behavior.error = QueryError(f"403 first {_PRIVATE}")
    first = _gather(scenario)
    scenario.behavior.error = QueryError(f"403 second {_PRIVATE}")
    repeated = _gather(scenario)
    scenario.behavior.error = QueryError(f"query timeout third {_PRIVATE}")
    distinct = _gather(scenario)
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert first.new_signals == 1 and repeated.new_signals == 0 and distinct.new_signals == 1
    assert len(signals) == 2 and len({signal.id for signal in signals}) == 2
    assert all(result.integration_error_count == 1 for result in (first, repeated, distinct))
    assert _PRIVATE not in repr(signals)


def test_same_time_replay_is_stable_and_later_failure_is_new(scenario: SimpleNamespace) -> None:
    first = _gather(scenario)
    initial = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    scenario.behavior.error = QueryError(f"429 another {_PRIVATE}")
    repeated = _gather(scenario)
    later = _gather(scenario, as_of=_NOW + timedelta(seconds=1))
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert len(initial) == 1 and initial[0].id == build_deterministic_signal_id(initial[0])
    assert first.new_signals == 1 and repeated.new_signals == 0 and later.new_signals == 1
    assert len(signals) == 2
    assert all(result.integration_error_count == 1 for result in (first, repeated, later))


def test_replay_deduplicates_an_existing_classified_diagnostic_with_legacy_id(scenario: SimpleNamespace) -> None:
    scenario.behavior.error = QueryError(_PRIVATE)
    signal = gather._build_integration_error_signal(
        program_id=_PROGRAM, source="kusto", error="Kusto gather failed", as_of=_NOW
    )
    legacy = gather.classify_signal(replace(signal, id=build_deterministic_signal_id(signal)))
    path = append_signal(legacy, programs_root=scenario.root, partition_at=_NOW)
    original = path.read_bytes()
    artifacts = _gather(scenario)
    assert artifacts.new_signals == 0 and path.read_bytes() == original
    stored = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert len(stored) == 1 and stored[0].id == legacy.id


def test_raw_legacy_diagnostic_is_not_rewritten(scenario: SimpleNamespace) -> None:
    scenario.behavior.error = QueryError(_PRIVATE)
    signal = gather._build_integration_error_signal(
        program_id=_PROGRAM, source="kusto", error=_PRIVATE, as_of=_NOW
    )
    legacy = gather.classify_signal(replace(signal, id=build_deterministic_signal_id(signal)))
    path = append_signal(legacy, programs_root=scenario.root, partition_at=_NOW)
    original = path.read_bytes()
    artifacts = _gather(scenario)
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))
    assert artifacts.new_signals == 1 and len(signals) == 2
    assert path.read_bytes().startswith(original)
    assert sum(_PRIVATE in signal.text for signal in signals) == 1


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_unexpected_error_still_propagates(scenario: SimpleNamespace, route: str) -> None:
    failure = RuntimeError("unexpected synthetic defect")
    scenario.behavior.route = route
    scenario.behavior.error = failure
    with pytest.raises(RuntimeError) as caught:
        _gather(scenario)
    assert caught.value is failure


@pytest.mark.parametrize("route", ["legacy", "uil"])
def test_successful_gather_remains_available(scenario: SimpleNamespace, route: str) -> None:
    scenario.behavior.route = route
    scenario.behavior.fail = False
    artifacts = _gather(scenario)
    assert scenario.calls == (["synthetic-query"] if route == "legacy" else ["uil"])
    assert artifacts.integration_errors == ()


def test_actual_cli_reports_failure_without_provider_detail(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario.behavior.route = "uil"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: object() if channel in ("ado", "kusto") else None,
    )
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--kusto"])
    assert result.exit_code == 2, result.output
    assert "kusto/gather" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output
