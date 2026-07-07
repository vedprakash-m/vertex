from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.gather_pipeline.models import (
    GatherArtifacts,
    M365PromotionBlockedArtifact,
    M365PromotionCandidate,
    StateWriteStageInput,
)
from src.commands.gather_pipeline.state_write_stage import run_state_write_stage
from src.core.models_v2 import IntegrationError


def _demo_input(tmp_path: Path) -> StateWriteStageInput:
    gathered_at = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    return StateWriteStageInput(
        program_id="demo",
        gathered_at=gathered_at,
        scanned_items=10,
        discovered_signals=7,
        new_signals=3,
        pending_review=2,
        trajectory_updates=4,
        auto_reviews_written=1,
        ado_calls=9,
        archived_journal_files=2,
        background_proposals=5,
        dependency_proposals_refreshed=6,
        integration_error_details=(
            IntegrationError(source="workiq", stage="discovery", retryable=False, message="timed out"),
        ),
        gather_flags={"workiq": True},
        channels={"ado": {"active": True}},
        m365_discovery={"active": True},
        previous_gathered_at=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
        previous_query_states={"q": {"ok": False}},
        previous_channels={"ado": {"active": False}},
        previous_m365_discovery={"active": False},
        query_states={"q": {"ok": True}},
        programs_root=tmp_path,
        promotion_candidates=(
            M365PromotionCandidate(
                artifact_id="thread:1",
                display_name="Demo",
                workstream_id="demo",
                confidence=0.9,
                signal_yield_last_3=(1, 2, 3),
            ),
        ),
        promotion_blocked_artifacts=(
            M365PromotionBlockedArtifact(
                artifact_id="thread:2",
                artifact_type="email_thread",
                display_name="Blocked",
                workstream_id="demo",
                blocker_reason="recent rejection",
            ),
        ),
        chart_results=("chart",),
        hypothesis_count=8,
    )


def test_run_state_write_stage_writes_state_and_returns_artifacts(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    calls: list[str] = []
    stage_input = _demo_input(tmp_path)

    monkeypatch.setattr(
        "src.commands.gather_pipeline.state_write_stage.compute_and_persist_plane1_changes",
        lambda program_id, programs_root, gathered_at: (
            calls.append("plane1"),
            captured.setdefault("plane1", (program_id, programs_root, gathered_at)),
        )[-1],
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.state_write_stage.write_gather_state",
        lambda program_id, **kwargs: (
            calls.append("write"),
            captured.setdefault("write", {"program_id": program_id, **kwargs}),
        )[-1],
    )

    result = run_state_write_stage(stage_input)

    assert captured["plane1"] == ("demo", tmp_path, stage_input.gathered_at)
    assert isinstance(result.artifacts, GatherArtifacts)
    assert result.artifacts.ado_calls == 9
    assert result.artifacts.promotion_candidates == stage_input.promotion_candidates
    assert result.artifacts.promotion_blocked_artifacts == stage_input.promotion_blocked_artifacts
    assert result.finalize_detail == "signals=7, new=3, hypotheses=8, ado_calls=9"
    assert calls == ["plane1", "write"]


def test_run_state_write_stage_passes_explicit_previous_state(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    stage_input = _demo_input(tmp_path)

    monkeypatch.setattr(
        "src.commands.gather_pipeline.state_write_stage.compute_and_persist_plane1_changes",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.state_write_stage.write_gather_state",
        lambda program_id, **kwargs: captured.setdefault("write", {"program_id": program_id, **kwargs}),
    )

    run_state_write_stage(stage_input)

    assert captured["write"] == {
        "program_id": "demo",
        "gathered_at": stage_input.gathered_at,
        "scanned_items": 10,
        "discovered_signals": 7,
        "new_signals": 3,
        "pending_review": 2,
        "trajectory_updates": 4,
        "auto_reviews_written": 1,
        "ado_calls": 9,
        "archived_journal_files": 2,
        "background_proposals": 5,
        "integration_errors": 1,
        "integration_error_details": stage_input.integration_error_details,
        "gather_flags": stage_input.gather_flags,
        "channels": stage_input.channels,
        "m365_discovery": stage_input.m365_discovery,
        "previous_gathered_at": stage_input.previous_gathered_at,
        "previous_query_states": stage_input.previous_query_states,
        "previous_channels": stage_input.previous_channels,
        "previous_m365_discovery": stage_input.previous_m365_discovery,
        "query_states": stage_input.query_states,
        "programs_root": tmp_path,
    }
