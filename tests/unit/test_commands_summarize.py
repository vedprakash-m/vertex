from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.summary_generator import RollingSummaryDraft
from src.commands import summarize
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Signal, SignalReviewDecision, TrajectoryPoint
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.summary_store import RollingSummary, load_summary, save_summary
from src.core.trajectory import append_trajectory_point


runner = CliRunner()


def test_summarize_command_writes_summary_from_approved_signals_only(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    approved_signal = Signal(
        id="sig-approved",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
        raw_ref="wi:1234",
        confidence=Confidence.HIGH,
    )
    unreviewed_signal = Signal(
        id="sig-unreviewed",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="WorkIQ summary that should stay out until reviewed.",
        raw_ref="msg:1",
        confidence=Confidence.MEDIUM,
    )
    append_signal(approved_signal, programs_root=programs_root, partition_at=fixed_now)
    append_signal(unreviewed_signal, programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-approved",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="priya@example.com",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="priya@example.com",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme",
        ),
        programs_root=programs_root,
    )

    fake_generator = _FakeSummaryGenerator(
        RollingSummaryDraft(
            text="## Current State\nApproved signal only.\n\n## New Since Last Summary\nFresh ADO evidence captured.\n\n## Risks And Watchouts\nETA drift remains open.",
            prompt_version="summary_generator.v1",
            word_count=19,
        )
    )
    monkeypatch.setattr(summarize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(summarize, "_build_summary_generator", lambda program: fake_generator)
    monkeypatch.setattr(summarize, "_now_utc", lambda: fixed_now)

    result = runner.invoke(app, ["summarize", "--program", "acme"])

    assert result.exit_code == 0
    summary = load_summary("acme", "acme", programs_root=programs_root)
    assert summary is not None
    assert "Approved signal only." in summary.text
    assert len(fake_generator.calls) == 1
    assert [signal.id for signal in fake_generator.calls[0]["signals"]] == ["sig-approved"]


def test_summarize_command_reads_signals_and_trajectories_from_sqlite_backend(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root, storage_backend="sqlite")
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    approved_signal = Signal(
        id="sig-approved",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
        raw_ref="wi:1234",
        confidence=Confidence.HIGH,
    )
    unreviewed_signal = Signal(
        id="sig-unreviewed",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="WorkIQ summary that should stay out until reviewed.",
        raw_ref="msg:1",
        confidence=Confidence.MEDIUM,
    )
    signal_store.append(approved_signal)
    signal_store.append(unreviewed_signal)
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="sig-approved",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="system",
        ),
    )
    trajectory_store.append(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="priya@example.com",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme",
        ),
    )
    trajectory_store.append(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="priya@example.com",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme",
        ),
    )

    fake_generator = _FakeSummaryGenerator(
        RollingSummaryDraft(
            text="## Current State\nApproved signal only.\n\n## New Since Last Summary\nFresh ADO evidence captured.\n\n## Risks And Watchouts\nETA drift remains open.",
            prompt_version="summary_generator.v1",
            word_count=19,
        )
    )
    monkeypatch.setattr(summarize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(summarize, "_build_summary_generator", lambda program: fake_generator)
    monkeypatch.setattr(summarize, "_now_utc", lambda: fixed_now)

    result = runner.invoke(app, ["summarize", "--program", "acme"])

    assert result.exit_code == 0
    summary = load_summary("acme", "acme", programs_root=programs_root)
    assert summary is not None
    assert "Approved signal only." in summary.text
    assert len(fake_generator.calls) == 1
    assert [signal.id for signal in fake_generator.calls[0]["signals"]] == ["sig-approved"]


def test_summarize_program_warns_when_summary_is_stale_and_no_new_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root)
    fixed_now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    save_summary(
        "acme",
        RollingSummary(
            workstream_id="acme",
            generated_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            prompt_version="summary_generator.v1",
            source_mode="incremental",
            signal_count=1,
            text="## Current State\nOld summary text.",
        ),
        programs_root=programs_root,
    )

    fake_generator = _FailingSummaryGenerator()

    artifacts = summarize.summarize_program(
        "acme",
        programs_root=programs_root,
        summary_builder=lambda program: fake_generator,
        now_provider=lambda: fixed_now,
    )

    assert artifacts.results[0].status == "unchanged"
    assert artifacts.warnings == (
        "Summary for acme is 29 days old and no new approved signals were available.",
    )


def test_summarize_program_reset_rewrites_only_targeted_workstream(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root, include_second_workstream=True)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    save_summary(
        "acme",
        RollingSummary(
            workstream_id="acme",
            generated_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            prompt_version="summary_generator.v1",
            source_mode="incremental",
            signal_count=1,
            text="old acme summary",
        ),
        programs_root=programs_root,
    )
    save_summary(
        "acme",
        RollingSummary(
            workstream_id="dd",
            generated_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            prompt_version="summary_generator.v1",
            source_mode="incremental",
            signal_count=1,
            text="old dd summary",
        ),
        programs_root=programs_root,
    )
    signal = Signal(
        id="sig-approved",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
        raw_ref="wi:1234",
        confidence=Confidence.HIGH,
    )
    append_signal(signal, programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-approved",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    fake_generator = _FakeSummaryGenerator(
        RollingSummaryDraft(
            text="## Current State\nReset summary for acme.",
            prompt_version="summary_generator.v1",
            word_count=7,
        )
    )

    artifacts = summarize.summarize_program(
        "acme",
        reset=True,
        target_workstream_id="acme",
        programs_root=programs_root,
        summary_builder=lambda program: fake_generator,
        now_provider=lambda: fixed_now,
    )

    assert len(artifacts.results) == 1
    assert artifacts.results[0].workstream_id == "acme"
    assert load_summary("acme", "acme", programs_root=programs_root).text == "## Current State\nReset summary for acme."
    assert load_summary("acme", "dd", programs_root=programs_root).text == "old dd summary"


def test_load_program_context_reads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "schema_version: '2.0'\nid: acme\nname: Acme\nstorage_backend: file\n",
        encoding="utf-8",
    )
    sentinel_snapshot = object()
    sentinel_workstream = object()
    captured: dict[str, object] = {}

    def _load_program_facts(program_id: str, *, programs_root: Path, fact_types: tuple[str, ...]):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        captured["fact_types"] = fact_types
        return sentinel_snapshot

    monkeypatch.setattr(summarize, "load_program_facts", _load_program_facts)
    monkeypatch.setattr(
        summarize,
        "project_workstreams",
        lambda snapshot: (sentinel_workstream,) if snapshot is sentinel_snapshot else (),
    )

    program, workstreams = summarize._load_program_context("acme", programs_root)

    assert program.id == "acme"
    assert workstreams == (sentinel_workstream,)
    assert captured == {
        "program_id": "acme",
        "programs_root": programs_root,
        "fact_types": ("workstream.entry",),
    }


def test_summarize_program_orders_signals_by_source_confidence_and_workiq_relevance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root)
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
  - alias: alex
    title: Senior Software Engineer
""".strip()
        + "\n",
    )
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    signals = (
        Signal(
            id="sig-workiq-low",
            timestamp=datetime(2026, 5, 10, 11, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="Alex asked for a status refresh.",
            raw_ref="msg:low",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "alex", "thread_id": "thread-low"},
            thread_id="thread-low",
        ),
        Signal(
            id="sig-workiq-high-older",
            timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="GM asked for blocker confirmation.",
            raw_ref="msg:high-1",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
            raw_ref="wi:1234",
            confidence=Confidence.HIGH,
        ),
        Signal(
            id="sig-workiq-high-newer",
            timestamp=datetime(2026, 5, 10, 10, 45, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="GM reiterated the blocker in the same thread.",
            raw_ref="msg:high-2",
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
    )
    for signal in signals:
        append_signal(signal, programs_root=programs_root, partition_at=fixed_now)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=fixed_now,
                reviewed_by="system",
            ),
            programs_root=programs_root,
        )

    fake_generator = _FakeSummaryGenerator(
        RollingSummaryDraft(
            text="## Current State\nRanked summary context.",
            prompt_version="summary_generator.v1",
            word_count=5,
        )
    )
    monkeypatch.setattr(summarize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(summarize, "_build_summary_generator", lambda program: fake_generator)
    monkeypatch.setattr(summarize, "_now_utc", lambda: fixed_now)

    artifacts = summarize.summarize_program(
        "acme",
        programs_root=programs_root,
        summary_builder=lambda program: fake_generator,
        now_provider=lambda: fixed_now,
    )

    assert artifacts.results[0].status == "written"
    assert [signal.id for signal in fake_generator.calls[0]["signals"]] == [
        "sig-ado",
        "sig-workiq-high-newer",
        "sig-workiq-high-older",
        "sig-workiq-low",
    ]


def test_summarize_command_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    first_summary_path = tmp_path / "acme.md"
    second_summary_path = tmp_path / "dd.md"
    monkeypatch.setattr(
        summarize,
        "summarize_program",
        lambda program, reset=False, target_workstream_id=None: summarize.SummaryArtifacts(
            program_id=program,
            results=(
                summarize.WorkstreamSummaryResult(
                    workstream_id="acme",
                    path=first_summary_path,
                    status="written",
                    signal_count=3,
                ),
                summarize.WorkstreamSummaryResult(
                    workstream_id="dd",
                    path=second_summary_path,
                    status="unchanged",
                    signal_count=0,
                    warning="Summary for dd is stale.",
                ),
            ),
            warnings=("Summary for dd is stale.",),
        ),
    )

    json_result = runner.invoke(app, ["summarize", "--program", "acme", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["written_count"] == 1
    assert payload["unchanged_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["warnings"] == ["Summary for dd is stale."]
    assert payload["results"][0]["path"] == str(first_summary_path)
    assert payload["results"][1]["warning"] == "Summary for dd is stale."

    csv_result = runner.invoke(app, ["summarize", "--program", "acme", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "row_type,program_id,workstream_id,status,signal_count,path,warning,written_count,unchanged_count,skipped_count"
    assert lines[1] == "summary,acme,,,,,,1,1,0"
    assert lines[2] == f"result,acme,acme,written,3,{first_summary_path},,,,"
    assert lines[3] == f"result,acme,dd,unchanged,0,{second_summary_path},Summary for dd is stale.,,,"
    assert lines[4] == "warning,acme,,,,,Summary for dd is stale.,,,"


def test_build_summary_generator_passes_trace_context_to_summary_generator(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    def _fake_from_program(program, *, trace_context=None):
        del program
        seen_trace_contexts.append(trace_context)
        return _FakeSummaryGenerator(
            RollingSummaryDraft(
                text="## Current State\nTrace-aware summary.",
                prompt_version="summary_generator.v1",
                word_count=4,
            )
        )

    monkeypatch.setattr(summarize.SummaryGenerator, "from_program", _fake_from_program)

    trace_context = summarize._build_summary_trace_context(
        program_id="acme",
        current_time=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        target_workstream_id=None,
        reset=False,
        budget_usd=0.5,
    )
    generator = summarize._build_default_summary_generator(
        program=summarize.Program(
            schema_version="2.0",
            id="acme",
            name="Acme Demo",
        ),
        trace_context=trace_context,
    )

    assert isinstance(generator, _FakeSummaryGenerator)
    assert seen_trace_contexts == [trace_context]
    assert trace_context.run_id == "acme:summarize:all:20260510T120000Z"
    assert trace_context.metadata["run_budget_usd"] == 0.5
    assert trace_context.metadata["task_type"] == "rolling_summary"


def test_build_default_summary_generator_returns_disabled_generator_without_calling_builder(monkeypatch) -> None:
    monkeypatch.setattr(
        summarize,
        "_build_summary_generator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_build_summary_generator should not be called")),
    )
    trace_context = summarize._build_summary_trace_context(
        program_id="acme",
        current_time=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        target_workstream_id=None,
        reset=False,
        budget_usd=0.5,
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        generator = summarize._build_default_summary_generator(
            program=summarize.Program(
                schema_version="2.0",
                id="acme",
                name="Acme Demo",
            ),
            trace_context=trace_context,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert isinstance(generator, summarize.SummaryGenerator)
    assert generator.generate(
        program=summarize.Program(schema_version="2.0", id="acme", name="Acme Demo"),
        workstream=summarize.Workstream(id="acme", name="Deployment Readiness", area_paths=("One\\Adventure\\Acme",)),
        prior_summary=None,
        signals=(),
        drift_patterns=(),
    ) is None


class _FakeSummaryGenerator:
    def __init__(self, draft: RollingSummaryDraft) -> None:
        self._draft = draft
        self.calls: list[dict[str, object]] = []

    def generate(self, *, program, workstream, prior_summary, signals, drift_patterns):
        self.calls.append(
            {
                "program": program,
                "workstream": workstream,
                "prior_summary": prior_summary,
                "signals": signals,
                "drift_patterns": drift_patterns,
            }
        )
        return self._draft


class _FailingSummaryGenerator:
    def generate(self, *, program, workstream, prior_summary, signals, drift_patterns):
        raise AssertionError("Summary generator should not run when no new approved signals exist.")


def _write_program_files(
    programs_root: Path,
    *,
    include_second_workstream: bool = False,
    storage_backend: str | None = None,
) -> None:
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    program_yaml = """
schema_version: "2.0"
id: acme
name: Acme Demo
ado:
  organization: your-org
  project: One
  area_paths: ['One\\Adventure\\Acme']
  work_item_types: ["Feature"]
  excluded_states: ["Removed"]
  date_window_days: 14
  api_timeout_seconds: 30
""".strip()
    if storage_backend is not None:
        program_yaml += f"\nstorage_backend: {storage_backend}"
    (program_dir / "program.yaml").write_text(program_yaml, encoding="utf-8")
    workstreams_yaml = [
        'schema_version: "2.0"',
        'workstreams:',
        '  - id: acme',
        '    name: Deployment Readiness',
        "    area_paths: ['One\\Adventure\\Acme']",
    ]
    if include_second_workstream:
        workstreams_yaml.extend(
            [
                '  - id: dd',
                '    name: DD on PF',
                "    area_paths: ['One\\Adventure\\Contoso']",
            ]
        )
    (program_dir / "workstreams.yaml").write_text("\n".join(workstreams_yaml), encoding="utf-8")


def _write_people_directory(knowledge_root: Path, content: str) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "people_directory.yaml").write_text(content, encoding="utf-8")