from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import cli
from typer.testing import CliRunner

from cli import app
from src.core.hypothesis_models import AssertionOperator, ChallengeKind, ChallengeSeverity, ChallengeState, Hypothesis, HypothesisKind, HypothesisStatus, RealityChallenge, TelemetryAssertion
from src.core.metric_models import MetricAggregation, ObservationWindow
from src.core.feedback.salience_modeler import SalienceEvent, append_salience_event, read_salience_events
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.reality_store import RealityStore


runner = CliRunner()


def test_resolve_program_for_session_catchup_prefers_program_flag() -> None:
    assert cli._resolve_program_for_session_catchup(["status", "--program", "acme"]) == "acme"


def test_resolve_program_for_session_catchup_uses_edition(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "resolve_edition_paths",
        lambda edition_id, programs_root=None: type("Resolved", (), {"program_id": "acme"})() if edition_id == "acme_weekly" else None,
    )

    assert cli._resolve_program_for_session_catchup(["status", "--edition", "acme_weekly"]) == "acme"


def test_maybe_run_session_catchup_invokes_runner_when_interactive(monkeypatch) -> None:
    calls: list[str] = []

    class _Stdout:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("src.commands.catchup.build_catchup_event_builder", lambda program_id, programs_root: (lambda signals: ()))
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda program_id, programs_root: (lambda signals: ()))
    monkeypatch.setattr(cli, "maybe_catchup", lambda program_id, programs_root, emit, event_builder: calls.append(program_id))
    monkeypatch.setattr(cli, "sys", type("SysModule", (), {"argv": ["vertex", "status", "--program", "acme"], "stdout": _Stdout()})())

    class _Ctx:
        invoked_subcommand = "status"

    cli._maybe_run_session_catchup(_Ctx(), no_catchup=False)

    assert calls == ["acme"]


def test_maybe_run_scheduled_db_maintenance_invokes_compaction_for_program(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", lambda program_id: calls.append(program_id))
    monkeypatch.setattr(cli, "sys", type("SysModule", (), {"argv": ["vertex", "status", "--program", "acme"]})())

    class _Ctx:
        invoked_subcommand = "status"

    cli._maybe_run_scheduled_db_maintenance(_Ctx())

    assert calls == ["acme"]


def test_maybe_run_scheduled_db_maintenance_skips_admin_db_commands(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", lambda program_id: calls.append(program_id))
    monkeypatch.setattr(cli, "sys", type("SysModule", (), {"argv": ["vertex", "admin", "db", "verify", "--program", "acme"]})())

    class _Ctx:
        invoked_subcommand = "admin"

    cli._maybe_run_scheduled_db_maintenance(_Ctx())

    assert calls == []


def test_catchup_cli_accepts_explicit_sources(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...], int | None]] = []

    monkeypatch.setattr(
        "src.commands.catchup._validate_catchup_sources",
        lambda program_id, selected_sources: None,
    )
    monkeypatch.setattr(
        "src.commands.catchup.run_catchup",
        lambda program, since_hours, sources, programs_root, event_builder, **kwargs: calls.append(
            (program, tuple(source.value for source in sources), since_hours)
        ) or type("Result", (), {"duration_seconds": 1.2, "result": type("Poll", (), {"since": None, "polled_at": None, "scanned_items": 0, "discovered_signals": 0, "new_signals": 0, "new_signal_summaries": ()})()})(),
    )
    monkeypatch.setattr("src.commands.catchup.build_catchup_event_builder", lambda program_id, programs_root: (lambda signals: ()))
    monkeypatch.setattr(
        "src.commands.catchup.render_catchup_banner",
        lambda result: "banner",
    )

    result = runner.invoke(app, ["catchup", "--program", "acme", "--source", "ado,workiq", "--since", "2"])

    assert result.exit_code == 0
    assert calls == [("acme", ("ado", "workiq"), 2)]


def test_catchup_cli_notify_emits_terminal_bell_for_l0_path(monkeypatch) -> None:
    bell_calls: list[bool] = []

    monkeypatch.setattr(
        "src.commands.catchup._validate_catchup_sources",
        lambda program_id, selected_sources: None,
    )
    monkeypatch.setattr(
        "src.commands.catchup.run_catchup",
        lambda program, since_hours, sources, programs_root, event_builder, **kwargs: type(
            "Result",
            (),
            {"duration_seconds": 1.2, "result": type("Poll", (), {"since": None, "polled_at": None, "scanned_items": 0, "discovered_signals": 0, "new_signals": 0, "new_signal_summaries": ()})()},
        )(),
    )
    monkeypatch.setattr("src.commands.catchup.build_catchup_event_builder", lambda program_id, programs_root: (lambda signals: ()))
    monkeypatch.setattr("src.commands.catchup.render_catchup_banner", lambda result: "banner")
    monkeypatch.setattr("src.commands.catchup._emit_terminal_bell", lambda enabled: bell_calls.append(enabled))

    result = runner.invoke(app, ["catchup", "--program", "acme", "--notify"])

    assert result.exit_code == 0
    assert bell_calls == [True]


def test_build_catchup_summary_builder_returns_classified_summaries(monkeypatch, tmp_path: Path) -> None:
    from src.commands.catchup import build_catchup_summary_builder
    from src.core.models import Confidence
    from src.core.models_v2 import Signal

    programs_root = tmp_path / "programs"
    monkeypatch.setattr(
        "src.commands.catchup.load_author_salience",
        lambda program_id, programs_root: None,
    )

    builder = build_catchup_summary_builder("acme", programs_root=programs_root)
    summaries = builder(
        (
            Signal(
                id="1",
                timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
                source="vertex/catchup",
                program_id="acme",
                workstream_id="deployment",
                entity_refs=("WI:1234",),
                text="raw text",
                raw_ref="wi:1234:rev:7:targetdate",
                confidence=Confidence.HIGH,
                metadata={
                    "work_item_id": 1234,
                    "field": "TargetDate",
                    "prior": "2026-06-15",
                    "current": "2026-06-22",
                },
            ),
        )
    )

    assert summaries == ("ETA slip: ADO#1234 moved from 2026-06-15 to 2026-06-22.",)


def test_build_catchup_event_builder_returns_typed_events(monkeypatch, tmp_path: Path) -> None:
    from src.commands.catchup import build_catchup_event_builder

    programs_root = tmp_path / "programs"
    monkeypatch.setattr(
        "src.commands.catchup.load_author_salience",
        lambda program_id, programs_root: None,
    )

    builder = build_catchup_event_builder("acme", programs_root=programs_root)
    events = builder(
        (
            Signal(
                id="1",
                timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
                source="vertex/catchup",
                program_id="acme",
                workstream_id="deployment",
                entity_refs=("WI:1234",),
                text="raw text",
                raw_ref="wi:1234:rev:7:targetdate",
                confidence=Confidence.HIGH,
                metadata={
                    "work_item_id": 1234,
                    "field": "TargetDate",
                    "prior": "2026-06-15",
                    "current": "2026-06-22",
                },
            ),
        )
    )

    assert len(events) == 1
    assert events[0].kind == "eta_slip"
    assert events[0].summary == "ETA slip: ADO#1234 moved from 2026-06-15 to 2026-06-22."


def test_build_catchup_event_builder_appends_confirmed_slip_for_matching_dismissal(monkeypatch, tmp_path: Path) -> None:
    from src.commands.catchup import build_catchup_event_builder

    programs_root = tmp_path / "programs"
    append_salience_event(
        "acme",
        SalienceEvent(
            event_id="dismissed-event-1",
            recorded_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            anomaly_id="sig-dismissed-1",
            workstream_id="deployment",
            action="dismissed",
            work_item_id=1234,
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        "src.commands.catchup.load_author_salience",
        lambda program_id, programs_root: None,
    )

    builder = build_catchup_event_builder("acme", programs_root=programs_root)
    events = builder(
        (
            Signal(
                id="sig-current-1",
                timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
                source="vertex/catchup",
                program_id="acme",
                workstream_id="deployment",
                entity_refs=("WI:1234",),
                text="raw text",
                raw_ref="wi:1234:rev:8:targetdate",
                confidence=Confidence.HIGH,
                metadata={
                    "work_item_id": 1234,
                    "field": "TargetDate",
                    "prior": "2026-06-15",
                    "current": "2026-06-22",
                },
            ),
        )
    )
    salience_events = read_salience_events("acme", programs_root=programs_root)

    assert len(events) == 1
    assert events[0].kind == "eta_slip"
    assert salience_events[-1].action == "confirmed_slip"
    assert salience_events[-1].anomaly_id == "sig-dismissed-1"
    assert salience_events[-1].work_item_id == 1234
    assert salience_events[-1].confirmed_within_30d is True
    assert salience_events[-1].decision_latency_ms is not None
    assert salience_events[-1].weight_before is not None
    assert salience_events[-1].weight_after is not None


def test_catchup_cli_reports_source_readiness_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.catchup._validate_catchup_sources",
        lambda program_id, selected_sources: (_ for _ in ()).throw(ValueError("unexpected")),
    )

    result = runner.invoke(app, ["catchup", "--program", "acme", "--source", "ado,workiq"])

    assert result.exit_code == 2
    assert "unexpected" in result.stdout


def test_catchup_cli_uses_l1_reality_path_for_iso_since(monkeypatch) -> None:
    from src.commands.catchup import RealityCatchupResult
    from src.core.hypothesis_models import DigestDelta

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "src.commands.catchup.run_reality_catchup",
        lambda program_id, since, reason, db_root=None: calls.append((program_id, reason))
        or RealityCatchupResult(
            program_id=program_id,
            since=since,
            as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
            reason=reason,
            delta=DigestDelta(
                since=since,
                to=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                challenges_opened=2,
                challenges_resolved=1,
                challenges_dismissed=0,
                challenges_snoozed=0,
                hypotheses_proposed=0,
                hypotheses_confirmed=0,
                hypotheses_recovered=1,
                hypotheses_superseded=0,
            ),
            resolved_naturally_count=1,
            still_open_count=1,
            resolved_staleness_challenge_ids=("challenge-001",),
        ),
    )
    monkeypatch.setattr("src.commands.catchup._append_catchup_audit_event", lambda store, payload: None)

    result = runner.invoke(app, ["catchup", "--program", "acme", "--since", "2026-05-10", "--reason", "PTO"])

    assert result.exit_code == 0
    assert calls == [("acme", "PTO")]
    assert "Reality catch-up - acme" in result.stdout
    assert "2 challenges opened, 1 resolved naturally on fresh data, 1 still open." in result.stdout


def test_catchup_cli_notify_emits_terminal_bell_for_l1_path(monkeypatch) -> None:
    from src.commands.catchup import RealityCatchupResult
    from src.core.hypothesis_models import DigestDelta

    bell_calls: list[bool] = []

    monkeypatch.setattr(
        "src.commands.catchup.run_reality_catchup",
        lambda program_id, since, reason, db_root=None: RealityCatchupResult(
            program_id=program_id,
            since=since,
            as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
            reason=reason,
            delta=DigestDelta(
                since=since,
                to=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                challenges_opened=0,
                challenges_resolved=0,
                challenges_dismissed=0,
                challenges_snoozed=0,
                hypotheses_proposed=0,
                hypotheses_confirmed=0,
                hypotheses_recovered=0,
                hypotheses_superseded=0,
            ),
            resolved_naturally_count=0,
            still_open_count=0,
            resolved_staleness_challenge_ids=(),
        ),
    )
    monkeypatch.setattr("src.commands.catchup._append_catchup_audit_event", lambda store, payload: None)
    monkeypatch.setattr("src.commands.catchup._emit_terminal_bell", lambda enabled: bell_calls.append(enabled))

    result = runner.invoke(app, ["catchup", "--program", "acme", "--since", "2026-05-10", "--notify"])

    assert result.exit_code == 0
    assert "Reality catch-up - acme" in result.stdout
    assert bell_calls == [True]


def test_run_reality_catchup_records_resolved_staleness_candidates(tmp_path: Path, monkeypatch) -> None:
    from src.commands.catchup import run_reality_catchup

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc).date(),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 10, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-open",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=100.0,
            expected_value=150.0,
            delta_magnitude=2.0,
            severity=ChallengeSeverity.ALERT,
            source="metric:acme.cluster_count",
            detected_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            current_state=ChallengeState.OPEN,
            state_changed_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-stale-resolved",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.STALENESS,
            observed_value=None,
            expected_value=None,
            delta_magnitude=None,
            severity=ChallengeSeverity.WARN,
            source="reconciler:staleness",
            detected_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            current_state=ChallengeState.RESOLVED,
            state_changed_at=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc),
            state_reason="staleness_recovered_on_reconcile",
        )
    )
    monkeypatch.setattr("src.commands.catchup.reconcile_reality", lambda **kwargs: None)
    monkeypatch.setattr("src.commands.catchup._build_delivery_date_snapshot_provider", lambda program_id: None)
    monkeypatch.setattr("src.commands.catchup._build_metric_definition_map", lambda as_of: {})
    monkeypatch.setattr("src.commands.catchup._load_expected_gather_cadence_hours", lambda program_id: None)

    result = run_reality_catchup(
        program_id="acme",
        since=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
        reason="PTO",
        db_root=tmp_path / "db",
        as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result.delta.challenges_opened == 2
    assert result.resolved_naturally_count == 1
    assert result.still_open_count == 1
    assert result.resolved_staleness_challenge_ids == ("challenge-stale-resolved",)


def test_catchup_cli_acknowledges_resolved_staleness_into_confirmation_log(monkeypatch, tmp_path: Path) -> None:
    from src.commands.catchup import RealityCatchupResult
    from src.core.hypothesis_models import DigestDelta

    monkeypatch.setattr(
        "src.commands.catchup.run_reality_catchup",
        lambda program_id, since, reason, db_root=None: RealityCatchupResult(
            program_id=program_id,
            since=since,
            as_of=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
            reason=reason,
            delta=DigestDelta(
                since=since,
                to=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                challenges_opened=1,
                challenges_resolved=1,
                challenges_dismissed=0,
                challenges_snoozed=0,
                hypotheses_proposed=0,
                hypotheses_confirmed=0,
                hypotheses_recovered=0,
                hypotheses_superseded=0,
            ),
            resolved_naturally_count=1,
            still_open_count=0,
            resolved_staleness_challenge_ids=("challenge-001",),
        ),
    )

    result = runner.invoke(
        app,
        ["catchup", "--program", "acme", "--since", "2026-05-10", "--interactive", "--db-root", str(tmp_path / "db")],
        input="y\n",
    )

    log_path = tmp_path / "db" / "acme" / "_confirmations.jsonl"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert result.exit_code == 0
    assert result.stdout.count("Reality catch-up - acme") == 1
    assert "Acknowledged 1 resolved staleness challenge(s)." in result.stdout
    assert [entry["event_type"] for entry in entries] == ["catchup_summary", "catchup_acknowledged"]
    assert entries[1]["challenge_ids"] == ["challenge-001"]