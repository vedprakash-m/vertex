from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai.action_extractor import ActionExtractorError
from src.commands.gather_pipeline import persistence_stage
from src.commands.gather_pipeline.models import PersistenceStageInput
from src.commands.gather_pipeline.persistence_stage import run_persistence_stage
from src.core.file_stores import FileSignalStore
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
    fail_staging_run,
    quarantine_abandoned_staging_runs,
)
from src.core.models import Confidence
from src.core.models_v2 import (
    ADOConfig,
    AIConfig,
    ActionItem,
    Program,
    Signal,
    SignalReviewDecision,
    TeamsChat,
    Workstream,
    WorkstreamSignalSources,
)
from src.core.signal_classification import classify_signal
from src.core.signal_dedup import build_deterministic_signal_id
from src.core.sqlite_stores import SQLiteSignalStore


def _demo_program() -> Program:
    return Program(
        schema_version="2.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )


def _demo_signal(*, current_time: datetime) -> Signal:
    return Signal(
        id="sig-1",
        timestamp=current_time,
        source="ado",
        program_id="demo",
        workstream_id="demo.slice",
        entity_refs=("WI:101",),
        text="Signal text",
        raw_ref="ado:101",
        confidence=Confidence.MEDIUM,
        metadata={},
    )


def test_run_persistence_stage_dry_run_skips_disk_writes(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    signal = _demo_signal(current_time=current_time)
    store = SimpleNamespace(
        append=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("append called during dry run")),
        append_review=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("append_review called during dry run")),
        read=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read called during dry run")),
        read_reviews=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read_reviews called during dry run")),
    )

    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.write_dedup_drop_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("drop log write called during dry run")),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.append_incident_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("incident write called during dry run")),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.append_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("action write called during dry run")),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.upsert_decisions",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("decision write called during dry run")),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.write_autonomy_audit_entries",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("audit write called during dry run")),
    )
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.signal_can_be_auto_approved", lambda signal: False)

    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(signal,),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            dry_run=True,
        )
    )

    assert result.new_signals == (signal,)
    assert result.pending_review == 1
    assert result.auto_reviews_written == 0


def test_run_persistence_stage_routes_only_non_meeting_signals_to_action_extractors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    meeting = replace(
        _demo_signal(current_time=current_time),
        id="meeting-1",
        source="m365/meeting_transcript",
        text="Action: Confirm checkpoint | owner=priya | wi=WI:101",
        raw_ref="meeting:1",
    )
    email = replace(
        _demo_signal(current_time=current_time),
        id="email-1",
        source="workiq/email",
        text="Need to send the status summary.",
        raw_ref="email:1",
    )
    program = replace(_demo_program(), ai=AIConfig(enabled=True, budget_usd_per_run=0.25))
    store = SimpleNamespace(
        append=Mock(),
        append_review=Mock(),
        read=Mock(return_value=()),
        read_reviews=Mock(return_value={}),
    )
    basic_extractor = Mock(return_value=())
    ai_extractor = Mock(return_value=())
    monkeypatch.setattr(persistence_stage, "extract_actions_from_signals", basic_extractor)
    monkeypatch.setattr(
        persistence_stage,
        "load_program_facts",
        lambda *_args, **_kwargs: SimpleNamespace(facts=()),
    )

    run_persistence_stage(
        PersistenceStageInput(
            program=program,
            program_id="demo",
            workstreams=(),
            candidate_signals=(meeting, email),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=ai_extractor,
        )
    )

    basic_extractor.assert_not_called()
    ai_extractor.assert_called_once_with(program, (email,))


def test_run_persistence_stage_canonicalizes_manifest_backed_signal_ids(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    signal = _demo_signal(current_time=current_time)
    store = SimpleNamespace()
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.signal_can_be_auto_approved", lambda signal: False)

    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(signal,),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            dry_run=True,
            gather_run_id="gather-committed-candidate",
        )
    )

    persisted = result.new_signals[0]
    assert persisted.id == build_deterministic_signal_id(persisted)
    assert persisted.gather_run_id == "gather-committed-candidate"
    assert signal.id == "sig-1"


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("source_kind", ["workiq", "fallback"])
def test_manifest_signal_dedup_preserves_prior_run_after_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_backend: str,
    source_kind: str,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    program = _demo_program()
    workstreams = (
        Workstream(
            id="demo.slice",
            name="Slice",
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Chat", work_item_ids=(101,)),),
            ),
        ),
    )
    signal = replace(
        _demo_signal(current_time=current_time),
        source="workiq/teams" if source_kind == "workiq" else "synthetic/source",
        entity_refs=("WS:demo.slice",) if source_kind == "workiq" else ("WI:101",),
        metadata={"message_id": "native-message-1"} if source_kind == "workiq" else {"event_id": "event-1"},
    )
    store = FileSignalStore(programs_root=tmp_path) if storage_backend == "file" else SQLiteSignalStore(programs_root=tmp_path)
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.signal_can_be_auto_approved", lambda signal: False)
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.compute_auto_approval_policies", lambda *_args: {})
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.load_program_facts", lambda *_args, **_kwargs: SimpleNamespace(facts=()))

    def manifest(run_id: str) -> GatherRunManifest:
        return GatherRunManifest(
            run_id=run_id,
            status=GatherRunStatus.RUNNING,
            program_id=program.id,
            actor_identity_type="interactive",
            lease_owner="test-owner",
            lease_fencing_token=1,
            started_at=current_time,
            scope_as_of=current_time,
            required_scope_status=RequiredScopeStatus.PARTIAL,
        )

    def persist(run_id: str, candidate: Signal, existing: tuple[Signal, ...]):
        return run_persistence_stage(
            PersistenceStageInput(
                program=program,
                program_id=program.id,
                workstreams=workstreams,
                candidate_signals=(candidate,),
                existing_signals=existing,
                signal_store=store,
                current_time=current_time,
                programs_root=tmp_path,
                ai_action_extractor=None,
                gather_run_id=run_id,
            )
        )

    prior = manifest("gather-prior")
    create_staging_manifest(prior, programs_root=tmp_path)
    first = persist(prior.run_id, signal, ())
    commit_staging_run(prior, finished_at=current_time, programs_root=tmp_path)
    assert len(first.new_signals) == 1
    before_retry = store.read(program.id)
    assert len(before_retry) == 1

    retry = manifest("gather-retry")
    create_staging_manifest(retry, programs_root=tmp_path)
    result = persist(retry.run_id, replace(signal, id="transient-retry"), before_retry)
    fail_staging_run(retry, finished_at=current_time, programs_root=tmp_path)

    assert result.new_signals == ()
    assert store.read(program.id) == before_retry
    assert before_retry[0].gather_run_id == prior.run_id
    assert before_retry[0].id == build_deterministic_signal_id(before_retry[0])


@pytest.mark.parametrize("prior_state", ["committed", "failed", "staged", "legacy", "unknown"])
@pytest.mark.parametrize("equivalent", [False, True])
def test_manifest_sqlite_collision_preserves_out_of_window_signal(
    tmp_path: Path, prior_state: str, equivalent: bool,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    candidate = classify_signal(_demo_signal(current_time=current_time - timedelta(days=90)))
    candidate = replace(candidate, id=build_deterministic_signal_id(candidate))
    run_id = None if prior_state == "legacy" else f"gather-prior-{prior_state}"
    prior = replace(
        candidate,
        text=candidate.text if equivalent else "Different retained evidence",
        gather_run_id=run_id,
    )
    store = SQLiteSignalStore(programs_root=tmp_path)
    if prior_state not in {"legacy", "unknown"}:
        manifest = GatherRunManifest(
            run_id=run_id,
            status=GatherRunStatus.RUNNING,
            program_id="demo",
            actor_identity_type="interactive",
            lease_owner="test-owner",
            lease_fencing_token=1,
            started_at=prior.timestamp,
            scope_as_of=prior.timestamp,
            required_scope_status=RequiredScopeStatus.PARTIAL,
        )
        create_staging_manifest(manifest, programs_root=tmp_path)
        if prior_state == "committed":
            commit_staging_run(manifest, finished_at=prior.timestamp, programs_root=tmp_path)
        elif prior_state == "failed":
            fail_staging_run(manifest, finished_at=prior.timestamp, programs_root=tmp_path)
    store.append(prior)
    before = store.read("demo")
    assert before == (prior,)
    assert store.read("demo", start=current_time - timedelta(days=14)) == ()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        run_persistence_stage(
            PersistenceStageInput(
                program=_demo_program(),
                program_id="demo",
                workstreams=(),
                candidate_signals=(candidate,),
                existing_signals=(),
                signal_store=store,
                current_time=current_time,
                programs_root=tmp_path,
                ai_action_extractor=None,
                gather_run_id="gather-attempt",
            )
        )

    assert store.read("demo") == before


@pytest.mark.parametrize("prior_state", ["failed", "quarantined"])
@pytest.mark.parametrize(
    ("prior_days", "candidate_days", "replaceable"),
    [
        (-15, -1, False),
        (1, -1, False),
        (-1, 1, False),
        (-1, -15, False),
        (-14, 0, True),
        (0, -14, True),
    ],
    ids=["prior-before", "prior-after", "candidate-after", "candidate-before", "boundary-start", "boundary-end"],
)
def test_manifest_retry_replaces_only_signals_within_full_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prior_state: str,
    prior_days: int,
    candidate_days: int,
    replaceable: bool,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    window_start = current_time - timedelta(days=14)
    prior = classify_signal(_demo_signal(current_time=current_time + timedelta(days=prior_days)))
    prior = replace(prior, id=build_deterministic_signal_id(prior), gather_run_id="gather-prior")
    candidate = replace(prior, timestamp=current_time + timedelta(days=candidate_days), gather_run_id=None)
    assert build_deterministic_signal_id(candidate) == prior.id
    unrelated = replace(candidate, entity_refs=("WI:102",), raw_ref="ado:102", text="Other evidence")

    manifest = GatherRunManifest(
        run_id="gather-prior",
        status=GatherRunStatus.RUNNING,
        program_id="demo",
        actor_identity_type="interactive",
        lease_owner="test-owner",
        lease_fencing_token=1,
        started_at=current_time,
        scope_as_of=current_time,
        required_scope_status=RequiredScopeStatus.PARTIAL,
    )
    create_staging_manifest(manifest, programs_root=tmp_path)
    if prior_state == "failed":
        fail_staging_run(manifest, finished_at=current_time, programs_root=tmp_path)
    else:
        quarantine_abandoned_staging_runs(
            "demo", finished_at=current_time, programs_root=tmp_path, is_lease_current_fn=lambda _: False,
        )
    store = SQLiteSignalStore(programs_root=tmp_path)
    store.append(prior)
    before = store.read("demo")
    stage_input = PersistenceStageInput(
        program=_demo_program(),
        program_id="demo",
        workstreams=(),
        candidate_signals=(unrelated, candidate),
        existing_signals=(),
        signal_store=store,
        current_time=current_time,
        programs_root=tmp_path,
        ai_action_extractor=None,
        gather_run_id="gather-retry",
        signal_window_start=window_start,
    )
    monkeypatch.setattr(persistence_stage, "signal_can_be_auto_approved", lambda _: False)
    monkeypatch.setattr(persistence_stage, "compute_auto_approval_policies", lambda *_: {})
    monkeypatch.setattr(persistence_stage, "load_program_facts", lambda *_args, **_kwargs: SimpleNamespace(facts=()))

    if replaceable:
        run_persistence_stage(stage_input)
        stored = store.read("demo")
        replacement = next(signal for signal in stored if signal.id == prior.id)
        assert replacement.timestamp == candidate.timestamp
        assert replacement.gather_run_id == "gather-retry"
        assert len(stored) == 2
    else:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            run_persistence_stage(stage_input)
        assert store.read("demo") == before


def test_atomic_retry_allows_an_omitted_window_end(tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    manifest = GatherRunManifest(
        run_id="gather-prior",
        status=GatherRunStatus.RUNNING,
        program_id="demo",
        actor_identity_type="interactive",
        lease_owner="test-owner",
        lease_fencing_token=1,
        started_at=current_time,
        scope_as_of=current_time,
        required_scope_status=RequiredScopeStatus.PARTIAL,
    )
    create_staging_manifest(manifest, programs_root=tmp_path)
    fail_staging_run(manifest, finished_at=current_time, programs_root=tmp_path)
    prior = replace(_demo_signal(current_time=current_time), gather_run_id=manifest.run_id)
    retry = replace(prior, timestamp=current_time + timedelta(days=1), gather_run_id="gather-retry")
    store = SQLiteSignalStore(programs_root=tmp_path)
    store.append(prior)

    store.append_atomic("demo", (retry,), replace_failed_since=current_time)

    assert store.read("demo") == (retry,)


def test_manifest_off_sqlite_collision_retains_replacement_behavior(tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    candidate = _demo_signal(current_time=current_time - timedelta(days=90))
    prior = replace(candidate, text="Older evidence", gather_run_id="historical-run")
    store = SQLiteSignalStore(programs_root=tmp_path)
    store.append(prior)

    run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(candidate,),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
        )
    )

    assert store.read("demo") == (classify_signal(candidate),)


def test_manifest_sqlite_collision_rolls_back_earlier_new_signal(tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    collision = classify_signal(_demo_signal(current_time=current_time - timedelta(days=90)))
    prior = replace(collision, id=build_deterministic_signal_id(collision), gather_run_id="prior-run")
    new = replace(collision, text="Different new evidence")
    store = SQLiteSignalStore(programs_root=tmp_path)
    store.append(prior)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        run_persistence_stage(
            PersistenceStageInput(
                program=_demo_program(),
                program_id="demo",
                workstreams=(),
                candidate_signals=(new, collision),
                existing_signals=(),
                signal_store=store,
                current_time=current_time,
                programs_root=tmp_path,
                ai_action_extractor=None,
                gather_run_id="failed-attempt",
            )
        )

    assert store.read("demo") == (prior,)


def test_manifest_persistence_keeps_custom_single_signal_store_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    signal = _demo_signal(current_time=current_time)
    written: list[tuple[Signal, bool]] = []
    store = SimpleNamespace(
        append=lambda entry, *, append_only=False: written.append((entry, append_only)),
        read=lambda *_args: (),
        read_reviews=lambda *_args: {},
    )
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.signal_can_be_auto_approved", lambda _signal: False)
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.compute_auto_approval_policies", lambda *_args: {})
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.load_program_facts", lambda *_args, **_kwargs: SimpleNamespace(facts=()))

    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(signal,),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            gather_run_id="gather-custom",
        )
    )

    assert written == [(classify_signal(result.new_signals[0]), True)]


def test_manifest_signal_dedup_normalizes_candidates_before_batch_comparison(tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    workstream = Workstream(
        id="demo.slice",
        name="Slice",
        signal_sources=WorkstreamSignalSources(
            teams_chats=(TeamsChat(display_name="Chat", work_item_ids=(101,)),),
        ),
    )
    signal = replace(
        _demo_signal(current_time=current_time),
        source="workiq/teams",
        entity_refs=("WS:demo.slice",),
        metadata={"message_id": "native-message-1"},
    )
    explicit = replace(signal, id="another-transient-id", entity_refs=("WS:demo.slice", "WI:101"))
    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(workstream,),
            candidate_signals=(signal, explicit),
            existing_signals=(),
            signal_store=SimpleNamespace(),
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            dry_run=True,
            gather_run_id="gather-batch",
        )
    )

    assert len(result.new_signals) == 1
    assert result.new_signals[0].entity_refs == ("WS:demo.slice", "WI:101")


@pytest.mark.parametrize("classified", [False, True])
def test_manifest_signal_dedup_matches_unclassified_legacy_records(
    tmp_path: Path, classified: bool,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    signal = replace(_demo_signal(current_time=current_time), source="synthetic/source")
    existing = classify_signal(signal) if classified else signal
    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(replace(signal, id="transient-retry"),),
            existing_signals=(existing,),
            signal_store=SimpleNamespace(),
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            dry_run=True,
            gather_run_id="gather-retry",
        )
    )

    assert result.new_signals == ()


@pytest.mark.parametrize("source_kind", ["workiq", "fallback"])
def test_manifest_signal_dedup_keeps_distinct_events_and_legacy_behavior(
    tmp_path: Path, source_kind: str,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    source = "workiq/teams" if source_kind == "workiq" else "synthetic/source"
    identity_field = "message_id" if source_kind == "workiq" else "event_id"
    first = replace(_demo_signal(current_time=current_time), source=source, metadata={identity_field: "one"})
    second = replace(first, id="sig-2", metadata={identity_field: "two"})
    stage_input = PersistenceStageInput(
        program=_demo_program(),
        program_id="demo",
        workstreams=(),
        candidate_signals=(first, second),
        existing_signals=(),
        signal_store=SimpleNamespace(),
        current_time=current_time,
        programs_root=tmp_path,
        ai_action_extractor=None,
        dry_run=True,
    )

    legacy = run_persistence_stage(stage_input)
    manifest = run_persistence_stage(replace(stage_input, gather_run_id="gather-batch"))
    assert tuple(signal.id for signal in legacy.new_signals) == ("sig-1", "sig-2")
    assert len(manifest.new_signals) == 2
    assert manifest.new_signals[0].id != manifest.new_signals[1].id


def test_run_persistence_stage_excludes_preview_only_workiq_signals(
    tmp_path: Path, caplog,
) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    preview = replace(
        _demo_signal(current_time=current_time),
        source="workiq/email",
        metadata={
            "message_id": "semantic:preview",
            "native_message_id": "semantic:preview",
            "source_identity_kind": "semantic_preview",
        },
    )
    result = run_persistence_stage(
        PersistenceStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            candidate_signals=(preview,),
            existing_signals=(),
            signal_store=SimpleNamespace(),
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
            dry_run=True,
        )
    )

    assert result.new_signals == ()
    assert result.pending_review == 0
    assert result.extracted_action_count == 0
    assert "semantic:preview" not in caplog.text


@pytest.mark.parametrize("error_type", [ActionExtractorError, RuntimeError])
@pytest.mark.parametrize("failure_point", ["setup", "extraction"])
@pytest.mark.parametrize("auto_approved", [False, True])
def test_ai_action_fallback_warns_without_program_or_exception_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    failure_point: str,
    auto_approved: bool,
) -> None:
    program_marker = "synthetic-private-program"
    error_marker = "synthetic-provider-private-detail"
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    program = replace(
        _demo_program(),
        id=program_marker,
        ai=AIConfig(enabled=True, budget_usd_per_run=0.25),
    )
    signal = replace(
        _demo_signal(current_time=current_time),
        program_id=program_marker,
        source="ado/revision" if auto_approved else "ado",
    )
    recorded: list[Signal] = []
    reviews: list[tuple[str, SignalReviewDecision]] = []
    store = SimpleNamespace(
        append=recorded.append,
        append_review=lambda program_id, review: reviews.append((program_id, review)),
        read=lambda *_args: tuple(recorded),
        read_reviews=lambda *_args: {},
    )
    extractor = SimpleNamespace(extract_actions=Mock())
    factory = Mock(return_value=extractor)
    if failure_point == "setup":
        factory.side_effect = error_type(error_marker)
    else:
        extractor.extract_actions.side_effect = error_type(error_marker)
    monkeypatch.setattr(persistence_stage.ActionExtractor, "from_program", factory)
    monkeypatch.setattr(persistence_stage, "load_program_facts", lambda *_args, **_kwargs: SimpleNamespace(facts=()))
    monkeypatch.setattr(persistence_stage, "compute_auto_approval_policies", lambda *_args: {})

    result = run_persistence_stage(
        PersistenceStageInput(
            program=program,
            program_id=program_marker,
            workstreams=(),
            candidate_signals=(signal,),
            existing_signals=(),
            signal_store=store,
            current_time=current_time,
            programs_root=tmp_path,
            ai_action_extractor=None,
        )
    )

    assert result.new_signals == (signal,)
    assert result.extracted_action_count == 0
    assert result.pending_review == int(not auto_approved)
    assert result.auto_reviews_written == int(auto_approved)
    assert recorded == [classify_signal(signal)]
    assert len(reviews) == int(auto_approved)
    if auto_approved:
        assert reviews[0][0] == program_marker
        assert reviews[0][1].signal_id == signal.id
        assert reviews[0][1].decision == "approved"
    factory.assert_called_once_with(program)
    if failure_point == "extraction":
        extractor.extract_actions.assert_called_once_with(program_id=program_marker, signals=(signal,))
    warnings = [record for record in caplog.records if record.name == persistence_stage.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    assert warnings[0].getMessage() == "AI action extraction skipped due to an error."
    assert warnings[0].exc_info is None
    assert program_marker not in repr(warnings[0].__dict__)
    assert error_marker not in repr(warnings[0].__dict__)


def test_ai_action_extraction_preserves_success_and_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = replace(_demo_program(), ai=AIConfig(enabled=True, budget_usd_per_run=0.25))
    signal = _demo_signal(current_time=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc))
    action = Mock(spec=ActionItem)
    extractor = SimpleNamespace(extract_actions=Mock(return_value=(action,)))
    factory = Mock(return_value=extractor)
    monkeypatch.setattr(persistence_stage.ActionExtractor, "from_program", factory)

    assert persistence_stage.extract_actions_with_ai(program, (signal,)) == (action,)
    unexpected = ValueError("synthetic unexpected error")
    extractor.extract_actions.side_effect = unexpected
    with pytest.raises(ValueError) as raised:
        persistence_stage.extract_actions_with_ai(program, (signal,))
    assert raised.value is unexpected
    assert persistence_stage.extract_actions_with_ai(program, ()) == ()
    assert persistence_stage.extract_actions_with_ai(replace(program, ai=None), (signal,)) == ()
    assert factory.call_count == 2


def test_run_persistence_stage_surfaces_auto_enforcement_errors(monkeypatch, tmp_path: Path) -> None:
    """WS-13 PB-5: governance writes (autonomy audit) MUST be loud. A failure
    in `compute_auto_approval_policies` (or downstream audit write) used to
    be silently swallowed by a bare `except Exception: pass`. The new
    contract: re-raise with a structured log line so the gather caller
    can record a degraded entry + force/waiver the cycle.
    """
    import pytest

    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    signal = _demo_signal(current_time=current_time)
    store = SimpleNamespace(
        append=lambda signal: None,
        append_review=lambda program_id, review: None,
        read=lambda program_id: (signal,),
        read_reviews=lambda program_id: {},
    )

    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.extract_actions_from_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.extract_decisions_from_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr("src.commands.gather_pipeline.persistence_stage.signal_can_be_auto_approved", lambda signal: True)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.compute_auto_approval_policies",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("policy failure")),
    )

    with pytest.raises(Exception, match="policy failure"):
        run_persistence_stage(
            PersistenceStageInput(
                program=_demo_program(),
                program_id="demo",
                workstreams=(),
                candidate_signals=(signal,),
                existing_signals=(),
                signal_store=store,
                current_time=current_time,
                programs_root=tmp_path,
                ai_action_extractor=None,
            )
        )
