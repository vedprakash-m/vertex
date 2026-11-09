from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
import typer
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import icm_signal_stage
from src.core.exceptions import AuthError, CredentialExpired, QueryError, QueryTimeoutError
from src.core.failure_taxonomy import FailureCategory, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.journal import append_signal, read_signals
from src.core.knowledge_store import KnowledgeStore
from src.core.models import Confidence
from src.core.models_v2 import ADOConfig, KustoConfig, KustoQuery, M365Config, Program, Workstream
from src.core.reality_store import RealityStore
from src.core.signal_dedup import build_deterministic_signal_id


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PROGRAM_ID = "synthetic-program-sentinel"
_PRIVATE = "SYNTHETICPRIVATEPROVIDERDETAIL"
_CONNECTOR = "SYNTHETICPRIVATECONNECTOR"
_AUTH_METHOD = "SYNTHETICPRIVATEAUTHMETHOD"


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "programs"
    db_root = tmp_path / "db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    program = Program(
        schema_version="2.0",
        id=_PROGRAM_ID,
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
        id="synthetic-icm",
        cluster="https://example.test",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Incidents",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM_ID,),
    )
    behavior = SimpleNamespace(route="agency", error=QueryError(_PRIVATE), empty=False)
    calls: list[str] = []

    class Bridge:
        def probe(self) -> SimpleNamespace:
            calls.append("probe")
            if behavior.route == "agency" and not behavior.empty:
                raise behavior.error
            return SimpleNamespace(
                available=behavior.route == "agency",
                has_icm=True,
                server_tools={"icm": ("list_incidents",)},
            )

        def invoke_mcp_tool(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("agency")
            return {"items": []}

    def resolve_binding(_program: object, _workstreams: object, channel: str, **_kwargs: object) -> object | None:
        return object() if channel == "icm" and behavior.route == "uil" else None

    def fail_uil(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append("uil")
        raise behavior.error

    def execute(_query: KustoQuery) -> list[dict[str, Any]]:
        calls.append("kusto")
        raise behavior.error

    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda *_args, **_kwargs: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=()
        ),
    )
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", resolve_binding)
    monkeypatch.setattr(gather, "_load_icm_signals_via_uil", fail_uil)
    monkeypatch.setattr(gather, "_load_kusto_queries", lambda *_args, **_kwargs: (query,))
    return SimpleNamespace(
        root=root,
        db_root=db_root,
        program=program,
        workstreams=workstreams,
        behavior=behavior,
        calls=calls,
        bridge=Bridge(),
        execute=execute,
    )


def _gather(scenario: SimpleNamespace, *, as_of: datetime = _NOW) -> Any:
    return gather.gather_program(
        _PROGRAM_ID,
        programs_root=scenario.root,
        as_of=as_of,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_icm=True,
        bridge_factory=lambda: scenario.bridge,
        kusto_query_executor=scenario.execute,
    )


@pytest.mark.parametrize("route", ["agency", "kusto", "uil"])
@pytest.mark.parametrize(
    "error_type", [AuthError, CredentialExpired, QueryError, QueryTimeoutError, TimeoutError, typer.BadParameter]
)
def test_actual_gather_surfaces_omit_provider_detail(
    scenario: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    route: str,
    error_type: type[Exception],
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = error_type(_PRIVATE)
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    state = load_gather_state(_PROGRAM_ID, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM_ID, programs_root=scenario.root)
    signals = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))
    store = RealityStore(_PROGRAM_ID, db_root=scenario.db_root)
    with sqlite3.connect(store.db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, error_message FROM reality_ingestion_runs WHERE source_ref = ?",
            ("icm",),
        ).fetchall()
    assert state is not None and manifest is not None
    assert scenario.calls == {
        "agency": ["probe"],
        "kusto": ["probe", "kusto"],
        "uil": ["uil"],
    }[route]
    assert artifacts.integration_error_count == 1
    detail = artifacts.integration_errors[0]
    assert (detail.source, detail.stage, detail.retryable) == ("icm", "gather", True)
    assert detail.message and detail.operator_action
    assert state.integration_error_details == (detail,)
    assert next(item for item in manifest.channel_outcomes if item.channel == "icm").degraded
    assert ingestion and all(row[0] == "failed" for row in ingestion)
    diagnostic = next(signal for signal in signals if (signal.metadata or {}).get("integration_source") == "icm")
    assert diagnostic.source == "system" and diagnostic.confidence == Confidence.NONE
    assert (diagnostic.metadata or {}).get("signal_class") == "status"
    assert artifacts.pending_review == 1 and artifacts.auto_reviews_written == 0
    assert gather._semantic_gather_exit_code(artifacts) == 2
    warnings = [record for record in caplog.records if record.getMessage().startswith("IcM gather failed")]
    assert len(warnings) == 1
    assert warnings[0].args == () and warnings[0].exc_info is None
    stderr = capsys.readouterr().err
    assert ("[ACTION REQUIRED]" in stderr) is (error_type is CredentialExpired)
    surfaces = {
        "gather result": repr(artifacts.integration_errors),
        "gather state": repr(state.integration_error_details),
        "manifest outcomes": repr(manifest.channel_outcomes),
        "manifest failed refs": repr(manifest.failed_refs),
        "persisted signals": repr(signals),
        "ingestion": repr(ingestion),
        "logs": caplog.text,
        "stderr": stderr,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert _PROGRAM_ID not in warnings[0].getMessage()


@pytest.mark.parametrize("route", ["agency", "kusto", "uil"])
def test_credential_expiry_banner_omits_provider_fields(
    scenario: SimpleNamespace, capsys: pytest.CaptureFixture[str], route: str
) -> None:
    scenario.behavior.route = route
    scenario.behavior.error = CredentialExpired(
        _PRIVATE, connector=_CONNECTOR, auth_method=_AUTH_METHOD
    )
    artifacts = _gather(scenario)
    stderr = capsys.readouterr().err
    assert artifacts.integration_error_count == 1
    assert "[ACTION REQUIRED]" in stderr
    assert "vertex doctor --adapter-cert" in stderr
    assert all(value not in stderr for value in (_PRIVATE, _CONNECTOR, _AUTH_METHOD))


def test_direct_failure_then_agency_failure_remains_degraded_and_private(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    program = replace(
        scenario.program,
        m365=M365Config(
            enabled=True, prefer_agency=True, icm_incidents_url="https://icm.example.test/incidents"
        ),
    )

    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            scenario.calls.append("direct-init")

        def list_incidents(self) -> NoReturn:
            scenario.calls.append("direct-query")
            raise QueryError(_PRIVATE)

    monkeypatch.setattr(icm_signal_stage, "IcmClient", DirectClient)
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, scenario.workstreams))
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    assert scenario.calls == ["direct-init", "direct-query", "probe"]
    assert artifacts.integration_error_count == 1
    assert gather._semantic_gather_exit_code(artifacts) == 2
    assert _PRIVATE not in caplog.text and _PROGRAM_ID not in caplog.text
    assert "Direct IcM query failed" in caplog.text and "IcM gather failed" in caplog.text


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
def test_observability_preserves_existing_failure_classification_without_provider_text(
    scenario: SimpleNamespace, text: str, category: FailureCategory
) -> None:
    error = f"{text} {_PRIVATE}"
    scenario.behavior.error = QueryError(error)
    expected = classify_exception(error)

    artifacts = _gather(scenario)
    report = build_diagnose_report(_PROGRAM_ID, programs_root=scenario.root)

    assert artifacts.integration_error_count == 1
    assert expected.category is category
    assert report.last_failure_category is category
    assert report.last_failure_retryable is expected.retryable
    assert report.last_failure_next_command == expected.next_command
    assert _PRIVATE not in repr(artifacts.integration_errors)
    assert _PRIVATE not in repr(report)


def test_taxonomy_pattern_details_are_safe_and_reclassify_consistently() -> None:
    from src.core.failure_taxonomy import _PATTERN_TABLE

    for _, patterns, _ in _PATTERN_TABLE:
        for pattern in patterns:
            original = classify_exception(f"{pattern} {_PRIVATE}")
            assert _PRIVATE not in original.detail
            safe_message = f"IcM gather failed; {original.detail}"
            replayed = classify_exception(safe_message)
            assert (replayed.category, replayed.retryable, replayed.next_command) == (
                original.category,
                original.retryable,
                original.next_command,
            )


def test_same_time_failures_deduplicate_by_safe_taxonomy_match(scenario: SimpleNamespace) -> None:
    scenario.behavior.error = QueryError(f"403 first {_PRIVATE}")
    first = _gather(scenario)
    scenario.behavior.error = QueryError(f"403 second {_PRIVATE}")
    repeated = _gather(scenario)
    scenario.behavior.error = QueryError(f"query timeout third {_PRIVATE}")
    distinct = _gather(scenario)
    signals = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))

    assert first.new_signals == 1 and repeated.new_signals == 0 and distinct.new_signals == 1
    assert len(signals) == 2 and len({signal.id for signal in signals}) == 2
    assert all(result.integration_error_count == 1 for result in (first, repeated, distinct))
    assert _PRIVATE not in repr(signals)


def test_same_time_failure_replay_is_stable_and_later_failure_is_new(scenario: SimpleNamespace) -> None:
    first = _gather(scenario)
    initial = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))
    scenario.behavior.error = QueryError("another synthetic provider error")
    repeated = _gather(scenario)
    replayed = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))
    state = load_gather_state(_PROGRAM_ID, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM_ID, programs_root=scenario.root)
    store = RealityStore(_PROGRAM_ID, db_root=scenario.db_root)
    with sqlite3.connect(store.db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, error_message FROM reality_ingestion_runs WHERE source_ref = ?",
            ("icm",),
        ).fetchall()
    later = _gather(scenario, as_of=_NOW + timedelta(seconds=1))
    later_signals = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))

    assert len(initial) == 1
    assert initial[0].id == build_deterministic_signal_id(initial[0])
    assert first.new_signals == 1 and repeated.new_signals == 0 and later.new_signals == 1
    assert len(replayed) == 1 and len(later_signals) == 2
    assert all(result.integration_error_count == 1 and gather._semantic_gather_exit_code(result) == 2
               for result in (first, repeated, later))
    assert state is not None and state.integration_errors == 1
    assert manifest is not None and next(item for item in manifest.channel_outcomes if item.channel == "icm").degraded
    assert ingestion and all(row == ("failed", "IcM gather failed") for row in ingestion)
    assert {signal.id for signal in initial} == {signal.id for signal in replayed}
    assert {signal.id for signal in initial} < {signal.id for signal in later_signals}


def test_replay_deduplicates_an_existing_classified_diagnostic_with_legacy_id(scenario: SimpleNamespace) -> None:
    signal = gather._build_integration_error_signal(
        program_id=_PROGRAM_ID, source="icm", error="IcM gather failed", as_of=_NOW
    )
    legacy = replace(signal, id=build_deterministic_signal_id(signal))
    legacy = gather.classify_signal(legacy)
    path = append_signal(legacy, programs_root=scenario.root, partition_at=_NOW)
    original = path.read_bytes()

    artifacts = _gather(scenario)

    assert artifacts.new_signals == 0
    assert path.read_bytes() == original
    stored = tuple(read_signals(_PROGRAM_ID, programs_root=scenario.root))
    assert len(stored) == 1 and stored[0].id == legacy.id


@pytest.mark.parametrize("route", ["agency", "kusto", "uil"])
def test_unexpected_error_still_propagates(scenario: SimpleNamespace, route: str) -> None:
    failure = RuntimeError("unexpected synthetic defect")
    scenario.behavior.route = route
    scenario.behavior.error = failure
    with pytest.raises(RuntimeError) as caught:
        _gather(scenario)
    assert caught.value is failure


def test_successful_empty_agency_response_is_not_a_failure(scenario: SimpleNamespace) -> None:
    scenario.behavior.empty = True
    artifacts = _gather(scenario)
    assert scenario.calls == ["probe", "agency"]
    assert artifacts.integration_errors == ()
    assert artifacts.discovered_signals == 0
    assert gather._semantic_gather_exit_code(artifacts) == 0


def test_actual_cli_reports_failure_without_provider_detail(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: object())
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM_ID, "--icm"])
    assert result.exit_code == 2, result.output
    assert "icm/gather" in result.output
    assert "Next:" in result.output
    assert _PRIVATE not in result.output
