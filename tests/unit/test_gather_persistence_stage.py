from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.gather_pipeline.models import PersistenceStageInput
from src.commands.gather_pipeline.persistence_stage import run_persistence_stage
from src.core.models import Confidence
from src.core.models_v2 import ADOConfig, Program, Signal
from src.core.signal_dedup import build_deterministic_signal_id


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
    assert persisted.id == build_deterministic_signal_id(signal)
    assert persisted.gather_run_id == "gather-committed-candidate"
    assert signal.id == "sig-1"


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
