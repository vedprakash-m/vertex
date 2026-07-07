from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.config_loader import ConfigError
from src.core.action_tracker import load_actions
from src.core.decision_register import load_decisions


runner = CliRunner()


def test_review_debrief_dry_run_does_not_write_state(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.review_debrief.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        [
            "review-debrief",
            "--program",
            "demo",
            "--title",
            "Confirm LT rollout stance",
            "--context",
            "LT asked for a narrower rollout recommendation.",
            "--decision",
            "Proceed with the guarded rollout.",
            "--action",
            "owner1|2026-05-30|Draft the narrower rollout note",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Review debrief preview" in result.stdout
    assert load_decisions("demo", programs_root=programs_root) == ()
    assert load_actions("demo", programs_root=programs_root) == ()


def test_review_debrief_writes_decision_and_follow_up_actions(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.review_debrief.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        [
            "review-debrief",
            "--program",
            "demo",
            "--issue",
            "78",
            "--title",
            "Confirm LT rollout stance",
            "--context",
            "LT asked for a narrower rollout recommendation.",
            "--decision",
            "Proceed with the guarded rollout.",
            "--reviewer",
            "operator",
            "--review-by",
            "2026-06-05",
            "--workstream",
            "deployment_readiness",
            "--entity-ref",
            "WI:1001",
            "--entity-ref",
            "WI:1002",
            "--alternative",
            "Pause the rollout",
            "--action",
            "owner1|2026-05-30|Draft the narrower rollout note",
            "--action",
            "owner2||Refresh the LT follow-up mail",
        ],
    )

    decisions = load_decisions("demo", programs_root=programs_root)
    actions = load_actions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Added debrief decision" in result.stdout
    assert "Added 2 follow-up action(s) to demo." in result.stdout
    assert len(decisions) == 1
    assert decisions[0].title == "Confirm LT rollout stance"
    assert decisions[0].decided_by == "operator"
    assert decisions[0].review_by == date(2026, 6, 5)
    assert decisions[0].workstream_id == "deployment_readiness"
    assert decisions[0].entity_refs == ("WI:1001", "WI:1002")
    assert "Debrief from issue 078" in decisions[0].context
    assert decisions[0].alternatives_considered == ("Pause the rollout",)
    assert len(actions) == 2
    assert {action.owner_alias for action in actions} == {"owner1", "owner2"}
    assert {action.source_type.value for action in actions} == {"review_feedback"}
    assert set(actions[0].linked_work_item_ids + actions[1].linked_work_item_ids) == {1001, 1002}


def test_review_debrief_records_trace_link_under_one_correlation_id(monkeypatch, tmp_path: Path) -> None:
    # ADF-W2.12: one debrief writes one decision plus N follow-up actions --
    # a real multi-fact chain worth tracing under one correlation id.
    from types import SimpleNamespace

    from src.core.operation_trace import load_operation_trace

    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.review_debrief.PROGRAMS_ROOT", programs_root)
    # review_debrief_command calls uuid4() once for decision_entry.id, then
    # once more for the new correlation_id -- fake sequential, distinct
    # values so we can identify the second (correlation_id) call's output.
    calls: list[str] = []

    def _fake_uuid4() -> SimpleNamespace:
        value = f"fake-uuid-{len(calls)}"
        calls.append(value)
        return SimpleNamespace(hex=value)

    monkeypatch.setattr("src.commands.review_debrief.uuid4", _fake_uuid4)

    result = runner.invoke(
        app,
        [
            "review-debrief",
            "--program", "demo",
            "--title", "Confirm LT rollout stance",
            "--context", "LT asked for a narrower rollout recommendation.",
            "--decision", "Proceed with the guarded rollout.",
            "--action", "owner1|2026-05-30|Draft the narrower rollout note",
            "--action", "owner2||Refresh the LT follow-up mail",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 2  # decision_entry.id, then correlation_id
    correlation_id = calls[1]
    trace = load_operation_trace("demo", correlation_id, programs_root=programs_root)
    assert trace is not None
    # 1 decision + 2 follow-up actions == 3 fact writes under one correlation id.
    assert len(trace.fact_refs) == 3


def test_review_debrief_uses_fact_projection_without_unrelated_action_loader(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.review_debrief.PROGRAMS_ROOT", programs_root)

    initial = runner.invoke(
        app,
        [
            "review-debrief",
            "--program",
            "demo",
            "--title",
            "Confirm LT rollout stance",
            "--context",
            "LT asked for a narrower rollout recommendation.",
            "--decision",
            "Proceed with the guarded rollout.",
        ],
    )
    assert initial.exit_code == 0

    def _fail_action_loader(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("actions unavailable")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _fail_action_loader)

    result = runner.invoke(
        app,
        [
            "review-debrief",
            "--program",
            "demo",
            "--title",
            "Confirm updated LT rollout stance",
            "--context",
            "LT confirmed the narrower rollout recommendation.",
            "--decision",
            "Proceed with the updated guarded rollout.",
        ],
    )

    decisions = load_decisions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(decisions) == 2
    assert {entry.title for entry in decisions} == {
        "Confirm LT rollout stance",
        "Confirm updated LT rollout stance",
    }