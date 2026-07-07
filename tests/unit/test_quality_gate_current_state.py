"""Guards the D-09 peel of current-state quality-gate helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus
from src.core.quality_gates import current_state as current_state_module


def test_current_state_loader_reads_actions_via_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    def _load_program_facts(program_id: str, *, programs_root, fact_types: tuple[str, ...]):
        captured.append((program_id, fact_types))
        return snapshot

    monkeypatch.setattr(
        current_state_module,
        "load_program_facts",
        _load_program_facts,
    )
    monkeypatch.setattr(
        current_state_module,
        "project_action_items",
        lambda loaded_snapshot: ("action-1",) if loaded_snapshot is snapshot else (),
    )

    actions = current_state_module.load_current_actions("acme", programs_root=tmp_path / "programs")

    assert actions == ("action-1",)
    assert captured == [("acme", ("action.item",))]


def test_current_state_loader_reads_dependencies_via_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    def _load_program_facts(program_id: str, *, programs_root, fact_types: tuple[str, ...]):
        captured.append((program_id, fact_types))
        return snapshot

    monkeypatch.setattr(
        current_state_module,
        "load_program_facts",
        _load_program_facts,
    )
    monkeypatch.setattr(
        current_state_module,
        "project_dependencies",
        lambda loaded_snapshot: ("dep-1",) if loaded_snapshot is snapshot else (),
    )

    dependencies = current_state_module.load_current_dependencies("acme", programs_root=tmp_path / "programs")

    assert dependencies == ("dep-1",)
    assert captured == [("acme", ("dependency.link",))]


def test_open_action_completeness_gate_flags_missing_owner_or_due_date(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        current_state_module,
        "load_current_actions",
        lambda program_id, *, programs_root: (
            ActionItem(
                id="A-1",
                program_id="acme",
                text="Close the loop",
                owner_alias="",
                due_date=date(2026, 6, 10),
                status=ActionStatus.OPEN,
                source_signal_id="signal-1",
                source_type=ActionSourceType.SIGNAL,
                linked_work_item_ids=(),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id=None,
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
            ActionItem(
                id="A-2",
                program_id="acme",
                text="Ship the fix",
                owner_alias="owner",
                due_date=None,
                status=ActionStatus.IN_PROGRESS,
                source_signal_id="signal-2",
                source_type=ActionSourceType.SIGNAL,
                linked_work_item_ids=(),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id=None,
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
        ),
    )

    result = current_state_module.evaluate_open_action_completeness_gate(
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert result.gate_id == "QG-15"
    assert result.passed is False
    assert "A-1" in result.message
    assert "A-2" in result.message
