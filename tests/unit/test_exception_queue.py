"""RELO-W4.12: deterministic read-only cockpit exception queue."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import click
from click.testing import CliRunner as ClickRunner
import dotenv
import pytest
from typer.main import get_command
from typer.testing import CliRunner

import src.ai.ai_mode as ai_mode
import src.commands.cockpit as cockpit_module
import src.commands.db as db_module
from src.commands.cockpit import app
from src.core.exception_queue import (
    DispatchException,
    ExceptionItem,
    build_exception_queue,
    load_dispatch_exceptions,
)
from src.core.jsonl_utils import append_jsonl_line
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.reality_store import RealityStore
from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore, build_natural_key
from src.core.program_reality import ProgramReality
from src.core.risk_register_engine import _risk_fact_payload, save_risk_register

AS_OF = date(2026, 11, 15)


def _risk(risk_id: str, *, reviewed: date, status: RiskStatus = RiskStatus.OPEN, assessed: bool = True) -> RiskEntry:
    return RiskEntry(
        id=risk_id, program_id="p", title=risk_id, description="",
        probability=RiskProbability.LIKELY if assessed else RiskProbability.UNASSESSED,
        impact=RiskImpact.HIGH if assessed else RiskImpact.UNASSESSED,
        category=RiskCategory.EXTERNAL, owner_alias="o", mitigation_plan=None, mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(), linked_claim_ids=(),
        linked_action_ids=(), status=status, identified_date=reviewed, identified_in_vertex_issue=None,
        last_reviewed_date=reviewed, entity_refs=(),
    )


def _action(action_id: str, *, due: date | None, status: ActionStatus = ActionStatus.OPEN) -> ActionItem:
    return ActionItem(
        id=action_id, program_id="p", text=action_id, owner_alias="o", due_date=due, status=status,
        source_signal_id=None, source_type=ActionSourceType.MANUAL, linked_work_item_ids=(), linked_claim_id=None,
        linked_risk_id=None, workstream_id=None, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        resolved_at=None, resolution_note=None,
    )


def test_queue_ranks_needs_input_then_overdue_actions_then_stale_risks() -> None:
    risks = (
        _risk("r-stale", reviewed=date(2026, 9, 1)),
        _risk("r-fresh", reviewed=date(2026, 11, 10)),
        _risk("r-unassessed", reviewed=date(2026, 11, 10), assessed=False),
        _risk("r-closed", reviewed=date(2025, 1, 1), status=RiskStatus.CLOSED),
    )
    actions = (
        _action("a-overdue", due=date(2026, 11, 1)),
        _action("a-done", due=date(2026, 1, 1), status=ActionStatus.DONE),
        _action("a-future", due=date(2026, 12, 1)),
    )
    queue = build_exception_queue(risks, actions, AS_OF)
    assert [(item.kind, item.subject_id) for item in queue] == [
        ("needs_input", "r-unassessed"),
        ("overdue_action", "a-overdue"),
        ("stale_risk", "r-stale"),
    ]
    assert queue[1] == ExceptionItem(
        kind="overdue_action", subject_id="a-overdue", severity_rank=1, age_days=14,
        reason="Action due 2026-11-01 is overdue.", evidence="actions.jsonl#a-overdue",
    )
    assert queue[2].evidence == "risk_register.yaml#r-stale"


def test_partial_dispatch_reconciliation_is_a_first_class_exception() -> None:
    dispatch = DispatchException(
        outbox_id="fixture-outbox",
        status="partial",
        age_days=2,
        reason="2 of 3 recipient effects are terminal; one remains uncertain.",
        evidence="actuation/outbox.db#fixture-outbox",
    )
    queue = build_exception_queue((), (), AS_OF, dispatch_exceptions=(dispatch,))
    assert queue == (
        ExceptionItem(
            kind="partial_dispatch",
            subject_id="fixture-outbox",
            severity_rank=0,
            age_days=2,
            reason=dispatch.reason,
            evidence=dispatch.evidence,
        ),
    )


def test_cli_exceptions_reads_partial_outbox_reconciliation_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from src.core.actuation_outbox import (
        DispatchResult,
        create_task_idempotency_key,
        dispatch_leased_create_task,
        enqueue_create_task_intent,
    )
    from src.core.actuation_reconciliation import EffectReconciliation
    from tests.support.actuation_reconciliation import synthetic_authorized_effect_document

    root = tmp_path / "programs"
    key = create_task_idempotency_key(
        program_id="synthetic",
        org="tenant",
        project="project",
        operation_intent_id="intent-1",
    )
    enqueue_create_task_intent(
        program_id="synthetic",
        idempotency_key=key,
        operation_intent_id="intent-1",
        proposal_id="proposal-1",
        payload_json=json.dumps({
            "operation": "create_task.v1",
            "operation_intent_id": "intent-1",
            "org": "org",
            "project": "project",
            "authorized_effect": synthetic_authorized_effect_document(
                program_id="synthetic",
                intent_id="intent-1",
            ),
        }),
        programs_root=root,
    )
    dispatch_leased_create_task(
        program_id="synthetic",
        idempotency_key=key,
        owner="synthetic-worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=True,
            remote_id=42,
            remote_rev=2,
            reconciliation=EffectReconciliation(
                status="partial",
                receipts=(),
                mismatches=("recipient bob: provider evidence missing",),
            ),
        ),
        programs_root=root,
    )
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = runner.invoke(
        app,
        ["exceptions", "--program", "synthetic", "--as-of", AS_OF.isoformat()],
    )

    assert result.exit_code == 0, result.output
    assert "[partial_dispatch]" in result.output
    assert key in result.output
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_cli_exceptions_includes_uncertain_dispatch_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from src.core.actuation_outbox import (
        create_task_idempotency_key,
        dispatch_leased_create_task,
        enqueue_create_task_intent,
    )
    from tests.support.actuation_reconciliation import synthetic_authorized_effect_document

    root = tmp_path / "programs"
    key = create_task_idempotency_key(
        program_id="synthetic",
        org="org",
        project="project",
        operation_intent_id="intent-timeout",
    )
    enqueue_create_task_intent(
        program_id="synthetic",
        idempotency_key=key,
        operation_intent_id="intent-timeout",
        proposal_id="proposal-1",
        payload_json=json.dumps({
            "operation": "create_task.v1",
            "operation_intent_id": "intent-timeout",
            "org": "org",
            "project": "project",
            "authorized_effect": synthetic_authorized_effect_document(
                program_id="synthetic",
                intent_id="intent-timeout",
            ),
        }),
        programs_root=root,
    )
    dispatch_leased_create_task(
        program_id="synthetic",
        idempotency_key=key,
        owner="synthetic-worker",
        dispatch_fn=lambda _entry: (_ for _ in ()).throw(TimeoutError("lost response")),
        programs_root=root,
    )
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)

    result = runner.invoke(
        app,
        ["exceptions", "--program", "synthetic", "--as-of", AS_OF.isoformat()],
    )

    assert result.exit_code == 0, result.output
    assert "[uncertain_dispatch]" in result.output
    assert "0 of 1 required recipient effects are terminal" in result.output


@pytest.mark.parametrize(
    "receipt_json",
    [
        "[]",
        '{"status": "partial", "receipts": {}}',
    ],
)
def test_dispatch_exception_refuses_malformed_retained_receipt(
    tmp_path: Path, receipt_json: str,
) -> None:
    from src.core._db import open_program_db_with_retry
    from src.core.actuation_outbox import outbox_db_path
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    db_path = outbox_db_path("synthetic", programs_root=root)
    entry = store.enqueue(
        db_path,
        "outbox-malformed",
        domain="actuation_dispatch",
        correlation_id="correlation-1",
        payload_json="{}",
    )
    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET status = 'uncertain_remote_state' WHERE outbox_id = ?",
            (entry.outbox_id,),
        )
        connection.execute(store._RECONCILIATION_RECEIPTS_SQL)
        connection.execute(
            "INSERT INTO outbox_reconciliation_receipts (outbox_id, receipt_json) VALUES (?, ?)",
            (entry.outbox_id, receipt_json),
        )
    with pytest.raises(ValueError, match="receipt"):
        load_dispatch_exceptions("synthetic", programs_root=root, as_of=AS_OF)


def test_dispatch_exception_refuses_naive_retained_timestamp(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry
    from src.core.actuation_outbox import outbox_db_path
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    db_path = outbox_db_path("synthetic", programs_root=root)
    entry = store.enqueue(
        db_path,
        "outbox-naive",
        domain="actuation_dispatch",
        correlation_id="correlation-1",
        payload_json="{}",
    )
    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET status = 'uncertain_remote_state', updated_at = '2026-11-15T00:00:00' "
            "WHERE outbox_id = ?",
            (entry.outbox_id,),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        load_dispatch_exceptions("synthetic", programs_root=root, as_of=AS_OF)


@pytest.mark.parametrize(
    ("reviewed", "expected_reason"),
    [
        (None, "Risk identified 2026-09-01; review date unavailable."),
        (date(2026, 10, 1), "Risk last reviewed 2026-10-01."),
    ],
)
def test_stale_risk_reason_distinguishes_identification_from_review(
    reviewed: date | None, expected_reason: str,
) -> None:
    risk = replace(_risk("r1", reviewed=date(2026, 9, 1)), last_reviewed_date=reviewed)

    queue = build_exception_queue((risk,), (), AS_OF)

    assert len(queue) == 1
    assert queue[0].reason == expected_reason


def test_queue_ties_break_by_age_then_subject_id_independent_of_input_order() -> None:
    actions = (
        _action("a-b", due=date(2026, 11, 1)),
        _action("a-old", due=date(2026, 10, 1)),
        _action("a-a", due=date(2026, 11, 1)),
    )
    expected = ["a-old", "a-a", "a-b"]
    assert [item.subject_id for item in build_exception_queue((), actions, AS_OF)] == expected
    assert [item.subject_id for item in build_exception_queue((), actions[::-1], AS_OF)] == expected


runner = CliRunner()


def test_cli_exceptions_discloses_risk_mirror_when_primary_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    mirror = replace(_risk("synthetic-risk", reviewed=AS_OF), program_id="synthetic", description="Synthetic risk")
    primary = replace(mirror, probability=RiskProbability.UNASSESSED)
    save_risk_register("synthetic", (mirror,), programs_root=root)
    recorded_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    refs = (f"RISK:{primary.id}",)
    ProgramFactStore("synthetic", db_root=root.parent).append_fact(
        ProgramFactInput(
            fact_type="risk.entry", scope="program", entity_refs=refs,
            payload=_risk_fact_payload(primary), precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key=build_natural_key("risk.entry", entity_refs=refs, scope="program"),
            created_by="synthetic-test",
        ),
        recorded_at=recorded_at,
    )
    save_fact_sor_state(
        "synthetic", mode="legacy", family_modes={"judgment": "primary"},
        recorded_at=recorded_at, recorded_by="synthetic-test", programs_root=root,
    )
    reality = ProgramReality.load("synthetic", programs_root=root, archive_root=None)
    assert reality.family_sor_mode("judgment") == "primary"
    primary_risks = tuple(assessment.record for assessment in reality.risks())
    assert [(item.kind, item.subject_id) for item in build_exception_queue(primary_risks, (), AS_OF)] == [
        ("needs_input", "synthetic-risk"),
    ]
    files_before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = runner.invoke(app, ["exceptions", "--program", "synthetic", "--as-of", AS_OF.isoformat()])

    assert result.exit_code == 0, result.output
    assert "[needs_input] synthetic-risk" not in result.output
    assert "Risk exceptions use the risk register; primary judgment facts are not evaluated." in result.output
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == files_before


def test_cli_exceptions_discloses_action_journal_rotation_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    journal = root / "synthetic" / "journal" / "actions.jsonl"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)

    def row(action_id: str, due: str) -> str:
        return json.dumps({
            "record_type": "action", "id": action_id, "program_id": "synthetic",
            "text": action_id, "owner_alias": "o", "status": "open", "source_type": "manual",
            "created_at": "2026-01-01T00:00:00+00:00", "due_date": due,
        }) + "\n"

    append_jsonl_line(journal, row("old", "2026-01-01"), max_bytes=1)
    args = ["exceptions", "--program", "synthetic", "--as-of", "2026-11-15"]
    before = runner.invoke(app, args)
    assert before.exit_code == 0, before.output
    assert "[overdue_action] old" in before.output
    assert append_jsonl_line(journal, row("new", "2027-01-01"), max_bytes=1)
    files_before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    after = runner.invoke(app, args)

    assert after.exit_code == 0, after.output
    assert "[overdue_action] old" not in after.output
    assert "No matching exceptions in available inputs." in after.output
    limitation = (
        "Only the current action journal is read; rotated or pruned actions and status updates may be omitted. "
        "Action statuses may be stale."
    )
    assert limitation in before.output
    assert limitation in after.output
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == files_before


def test_cli_exceptions_lists_ranked_queue_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(cockpit_module, "load_risk_register", lambda *_a, **_k: (_risk("r1", reviewed=date(2026, 9, 1)),))
    monkeypatch.setattr(cockpit_module, "load_actions", lambda *_a, **_k: (_action("a1", due=date(2026, 11, 1)),))
    monkeypatch.setattr(cockpit_module, "get_actions_path", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(cockpit_module, "get_risk_register_path", lambda *_a, **_k: tmp_path)
    result = runner.invoke(app, ["exceptions", "--program", "acme", "--as-of", "2026-11-15"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Exception queue (acme, as of 2026-11-15): 2"
    assert lines[1].startswith("  1. [overdue_action] a1")
    assert lines[2].startswith("  2. [stale_risk] r1")
    assert lines[3] == (
        "  Scope: unresolved dispatch reconciliation, unassessed risks, overdue actions, and stale risks only; "
        "other exceptions and input completeness are not evaluated."
    )
    assert lines[4] == "  Risk exceptions use the risk register; primary judgment facts are not evaluated."
    assert lines[5] == (
        "  Only the current action journal is read; rotated or pruned actions and status updates may be omitted. "
        "Action statuses may be stale."
    )
    assert lines[6] == "  Current files are evaluated using this date; historical state is not reconstructed."
    assert list(root.rglob("*")) == [root / "acme"]


def test_cli_exceptions_empty_program_reports_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    (root / "acme").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    result = runner.invoke(app, ["exceptions", "--program", "acme", "--as-of", "2026-11-15"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Exception queue (acme, as of 2026-11-15): 0",
        "  No matching exceptions in available inputs.",
        "  Inputs not detected when checked: risk register, action journal.",
        "  Scope: unresolved dispatch reconciliation, unassessed risks, overdue actions, and stale risks only; "
        "other exceptions and input completeness are not evaluated.",
        "  Risk exceptions use the risk register; primary judgment facts are not evaluated.",
        "  Only the current action journal is read; rotated or pruned actions and status updates may be omitted. "
        "Action statuses may be stale.",
        "  Current files are evaluated using this date; historical state is not reconstructed.",
    ]
    assert list(root.rglob("*")) == [root / "acme"]


@pytest.mark.parametrize("as_of", [None, "2026-01-01"])
def test_cli_exceptions_discloses_current_records_not_historical_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_of: str | None,
) -> None:
    root = tmp_path / "programs"
    (root / "synthetic").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(
        cockpit_module, "load_risk_register",
        lambda *_a, **_k: (_risk("r1", reviewed=date(2026, 11, 10), assessed=False),),
    )
    arguments = ["exceptions", "--program", "synthetic"]
    if as_of is not None:
        arguments.extend(["--as-of", as_of])

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    assert "[needs_input] r1" in result.output
    assert "Current files are evaluated using this date; historical state is not reconstructed." in result.output
    if as_of is not None:
        assert "(-313d)" in result.output


def test_cli_exceptions_defaults_to_current_utc_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    (root / "synthetic").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    clock = Mock()
    clock.now.return_value.date.return_value = AS_OF
    monkeypatch.setattr(cockpit_module, "datetime", clock)

    result = runner.invoke(app, ["exceptions", "--program", "synthetic"])

    assert result.exit_code == 0, result.output
    assert "Exception queue (synthetic, as of 2026-11-15): 0" in result.output
    clock.now.assert_called_once_with(timezone.utc)


@pytest.mark.parametrize("as_of", ["", "20261115", "2026-W46-7", "2026-02-30"])
def test_cli_exceptions_rejects_noncanonical_as_of_before_reading_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_of: str,
) -> None:
    root = tmp_path / "programs"
    (root / "synthetic").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    risk_read = Mock(side_effect=AssertionError("invalid date must not read risks"))
    action_read = Mock(side_effect=AssertionError("invalid date must not read actions"))
    monkeypatch.setattr(cockpit_module, "load_risk_register", risk_read)
    monkeypatch.setattr(cockpit_module, "load_actions", action_read)

    result = runner.invoke(app, ["exceptions", "--program", "synthetic", "--as-of", as_of])

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "--as-of must be an ISO date (YYYY-MM-DD)." in result.output
    risk_read.assert_not_called()
    action_read.assert_not_called()


@pytest.mark.parametrize(
    ("risk_present", "actions_present", "expected"),
    [
        (False, False, "  Inputs not detected when checked: risk register, action journal."),
        (False, True, "  Inputs not detected when checked: risk register."),
        (True, False, "  Inputs not detected when checked: action journal."),
        (True, True, None),
    ],
)
def test_cli_exceptions_reports_inputs_not_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    risk_present: bool,
    actions_present: bool,
    expected: str | None,
) -> None:
    root = tmp_path / "programs"
    program = root / "synthetic"
    program.mkdir(parents=True)
    if risk_present:
        (program / "risk_register.yaml").write_text("schema_version: '1.0'\nrisks: []\n", encoding="utf-8")
    if actions_present:
        (program / "journal").mkdir()
        (program / "journal" / "actions.jsonl").write_text("", encoding="utf-8")
    before = {path.relative_to(root) for path in root.rglob("*")}
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)

    result = runner.invoke(app, ["exceptions", "--program", "synthetic", "--as-of", "2026-11-15"])

    assert result.exit_code == 0, result.output
    diagnostics = [line for line in result.output.splitlines() if "Inputs not detected when checked:" in line]
    assert diagnostics == ([expected] if expected else [])
    assert {path.relative_to(root) for path in root.rglob("*")} == before


def test_cli_exceptions_retains_results_when_risk_appears_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    (root / "synthetic").mkdir(parents=True)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(
        cockpit_module, "load_risk_register",
        lambda *_a, **_k: (_risk("r1", reviewed=date(2026, 9, 1)),),
    )

    result = runner.invoke(app, ["exceptions", "--program", "synthetic", "--as-of", "2026-11-15"])

    assert result.exit_code == 0, result.output
    assert "1. [stale_risk] r1" in result.output
    assert "Inputs not detected when checked: risk register, action journal." in result.output


@pytest.mark.parametrize("invalid_input", ["risk", "actions"])
def test_cli_exceptions_does_not_treat_invalid_inputs_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_input: str,
) -> None:
    root = tmp_path / "programs"
    program = root / "synthetic"
    (program / "journal").mkdir(parents=True)
    (program / "risk_register.yaml").write_text(
        "[" if invalid_input == "risk" else "schema_version: '1.0'\nrisks: []\n", encoding="utf-8"
    )
    (program / "journal" / "actions.jsonl").write_text(
        "not-json\n" if invalid_input == "actions" else "", encoding="utf-8"
    )
    before = {path: path.read_bytes() for path in program.rglob("*") if path.is_file()}
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)

    result = runner.invoke(app, ["exceptions", "--program", "synthetic", "--as-of", "2026-11-15"])

    assert result.exit_code != 0
    assert "No matching exceptions in available inputs." not in result.output
    assert {path: path.read_bytes() for path in program.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_root_cli_exceptions_does_not_run_mutating_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separator: list[str],
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = tmp_path / "programs"
    (root / "synthetic").mkdir(parents=True)
    store = RealityStore("synthetic", db_root=tmp_path / "db")
    store.initialize()
    before = store.db_path.read_bytes()
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(db_module, "load_metric_definition_map", lambda **_kwargs: {})
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda *_args, **_kwargs: None)
    catchup = Mock()
    tier_sink = Mock()
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", tier_sink)
    monkeypatch.setattr(cli.sys, "argv", ["vertex", "cockpit", *separator, "exceptions"])
    monkeypatch.setattr(ai_mode, "_CURRENT_MODE", ai_mode.AIMode.DISABLED)

    result = runner.invoke(
        cli.app, ["cockpit", *separator, "exceptions", "--program", "synthetic", "--as-of", "2026-11-15"],
    )

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Exception queue (synthetic, as of 2026-11-15): 0" in result.output
    assert not (store.db_path.parent / ".compact_last_run").exists()
    assert store.db_path.read_bytes() == before
    catchup.assert_not_called()
    tier_sink.assert_not_called()
    assert ai_mode._CURRENT_MODE is ai_mode.AIMode.DISABLED


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (["cockpit", "exceptions", "--help"], 0),
        (["cockpit", "exceptions", "--program", "synthetic", "--as-of", "invalid"], 2),
        (["cockpit", "exceptions", "--program", "synthetic", "--invalid-option"], 2),
        (["cockpit", "exceptions"], 2),
        (["--skip-issue", "cockpit", "exceptions", "--program", "synthetic"], 2),
    ],
)
def test_root_cli_exception_validation_has_no_startup_effects(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], exit_code: int,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    hooks = [Mock(side_effect=AssertionError("Exception inspection must not run startup effects")) for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    monkeypatch.setattr(cli.sys, "argv", ["vertex", *arguments])
    monkeypatch.setattr(ai_mode, "_CURRENT_MODE", ai_mode.AIMode.DISABLED)

    result = runner.invoke(cli.app, arguments)

    assert result.exit_code == exit_code, result.output + repr(result.exception)
    for hook in hooks:
        hook.assert_not_called()
    assert ai_mode._CURRENT_MODE is ai_mode.AIMode.DISABLED


def test_other_cockpit_commands_preserve_root_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    cockpit = root.commands["cockpit"]
    assert isinstance(cockpit, click.Group)
    handler = Mock()
    cockpit.commands["fixture-operation"] = click.Command(
        "fixture-operation", callback=handler, params=[click.Option(["--program"])],
    )
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    monkeypatch.setattr(cli.sys, "argv", ["vertex", "cockpit", "fixture-operation"])
    monkeypatch.setattr(ai_mode, "_CURRENT_MODE", ai_mode.AIMode.DISABLED)

    result = ClickRunner().invoke(root, ["cockpit", "fixture-operation", "--program", "exceptions"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(program="exceptions")
    for hook in hooks:
        hook.assert_called_once()
    assert ai_mode._CURRENT_MODE is ai_mode.AIMode.ACTIVE
