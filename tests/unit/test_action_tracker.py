from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.action_tracker import assess_action_staleness, append_action, associate_action_with_work_item, build_action_id, load_actions, match_action_to_ado_update, update_action_status
from src.core.exceptions import StateError
from src.core.fact_lineage_coverage import has_fact_provenance
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, TrajectoryPoint
from src.core.operation_trace import load_operation_trace
from src.core.program_fact_store import load_program_facts, project_action_items


@pytest.mark.parametrize("operation", ["log", "latest", "load", "status", "associate"])
@pytest.mark.parametrize("bad_row", [b'{"synthetic-private"\n', b'["synthetic-private"]\n', b"\xff\n"])
def test_selected_action_reads_and_updates_refuse_corrupt_history(
    tmp_path: Path, operation: str, bad_row: bytes,
) -> None:
    from src.core.action_tracker import _action_to_record, load_latest_action_statuses, read_action_log

    action = _sample_action(status=ActionStatus.OPEN)
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    original = (json.dumps(_action_to_record(action)) + "\n").encode() + bad_row
    path.write_bytes(original)

    with pytest.raises(StateError, match="Action journal") as error:
        if operation == "log":
            read_action_log("demo", programs_root=tmp_path, repair_corruption=False)
        elif operation == "latest":
            load_latest_action_statuses("demo", programs_root=tmp_path, repair_corruption=False)
        elif operation == "load":
            load_actions("demo", programs_root=tmp_path, repair_corruption=False)
        elif operation == "status":
            update_action_status("demo", action.id, ActionStatus.DONE, None, updated_by="owner", programs_root=tmp_path)
        else:
            associate_action_with_work_item("demo", action.id, 2002, programs_root=tmp_path)

    assert "synthetic-private" not in str(error.value)
    assert path.read_bytes() == original
    assert not (path.parent / "quarantine").exists()
    assert not path.with_suffix(".sha256").exists()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == ["demo/journal/actions.jsonl"]


@pytest.mark.parametrize("operation", ["log", "load", "status", "associate"])
@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_nonrepairing_action_read_reports_unreadable_history_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: type[OSError],
) -> None:
    from src.core.action_tracker import _action_to_record, read_action_log

    action = _sample_action(status=ActionStatus.OPEN)
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    original = (json.dumps(_action_to_record(action)) + "\n").encode()
    path.write_bytes(original)
    original_read = Path.read_text

    def unreadable(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            raise failure("synthetic-private failure")
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(StateError, match="Action journal") as error:
        if operation == "log":
            read_action_log("demo", programs_root=tmp_path, repair_corruption=False)
        elif operation == "load":
            load_actions("demo", programs_root=tmp_path, repair_corruption=False)
        elif operation == "status":
            update_action_status("demo", action.id, ActionStatus.DONE, None, updated_by="owner", programs_root=tmp_path)
        else:
            associate_action_with_work_item("demo", action.id, 2002, programs_root=tmp_path)
    assert "synthetic-private" not in str(error.value)
    assert str(path) not in str(error.value)
    assert path.read_bytes() == original
    assert not (path.parent / "quarantine").exists()


def test_default_action_read_preserves_original_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    original_read = Path.read_text

    def unreadable(target: Path, *args: object, **kwargs: object) -> str:
        if target == path:
            raise PermissionError("original read error")
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(PermissionError, match="original read error"):
        load_actions("demo", programs_root=tmp_path)


def test_default_action_read_still_short_circuits_missing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.action_tracker import read_action_log

    def unexpected_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("A missing journal should not reach the JSONL reader.")

    monkeypatch.setattr("src.core.action_tracker.read_jsonl_records", unexpected_read)
    assert read_action_log("demo", programs_root=tmp_path) == ()
    assert load_actions("demo", programs_root=tmp_path) == ()


def test_nonrepairing_action_read_preserves_absent_and_valid_history(tmp_path: Path) -> None:
    from src.core.action_tracker import read_action_log

    assert load_actions("demo", programs_root=tmp_path, repair_corruption=False) == ()
    associate_action_with_work_item("demo", "missing", 2002, programs_root=tmp_path)
    assert tuple(tmp_path.iterdir()) == ()
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert load_actions("demo", programs_root=tmp_path, repair_corruption=False) == ()
    action = _sample_action()
    append_action("demo", action, programs_root=tmp_path)
    assert load_actions("demo", programs_root=tmp_path, repair_corruption=False) == (action,)
    assert read_action_log("demo", programs_root=tmp_path, repair_corruption=False) == (action,)


def test_associate_preserves_existing_empty_journal_behavior(tmp_path: Path) -> None:
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")

    associate_action_with_work_item("demo", "missing", 2002, programs_root=tmp_path)

    assert path.read_bytes() == b""
    assert path.with_suffix(".sha256").exists()


def test_nonrepairing_action_load_checks_the_status_read_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.action_tracker import _action_to_record

    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    original = json.dumps(_action_to_record(_sample_action())) + "\n"
    path.write_bytes(original.encode())
    original_read = Path.read_text
    reads = 0

    def change_on_second_read(target: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        if target == path:
            reads += 1
            if reads == 2:
                return original + '["synthetic-private"]\n'
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", change_on_second_read)
    with pytest.raises(StateError, match="Action journal") as error:
        load_actions("demo", programs_root=tmp_path, repair_corruption=False)
    assert reads == 2
    assert "synthetic-private" not in str(error.value)
    assert path.read_bytes() == original.encode()
    assert not (path.parent / "quarantine").exists()


def test_nonrepairing_action_load_uses_only_selected_program_root(tmp_path: Path) -> None:
    from src.core.action_tracker import _action_to_record

    selected_root = tmp_path / "selected" / "programs"
    other_root = tmp_path / "other" / "programs"
    action = _sample_action()
    selected_path = selected_root / "demo" / "journal" / "actions.jsonl"
    other_path = other_root / "demo" / "journal" / "actions.jsonl"
    selected_path.parent.mkdir(parents=True)
    other_path.parent.mkdir(parents=True)
    selected_path.write_text(json.dumps(_action_to_record(action)) + "\n", encoding="utf-8")
    other_path.write_bytes(b'["synthetic-private"]\n')

    assert load_actions("demo", programs_root=selected_root, repair_corruption=False) == (action,)
    assert other_path.read_bytes() == b'["synthetic-private"]\n'
    assert not (other_path.parent / "quarantine").exists()


def test_append_action_rejects_foreign_program_before_writes(tmp_path: Path) -> None:
    action = replace(_sample_action(), program_id="other")
    with pytest.raises(ValueError, match="Action program does not match selected program"):
        append_action("demo", action, programs_root=tmp_path)
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("operation", ["load", "log", "status", "associate"])
def test_foreign_action_history_is_rejected_without_mutation(tmp_path: Path, operation: str) -> None:
    from src.core.action_tracker import read_action_log, _action_to_record

    action = replace(_sample_action(status=ActionStatus.OPEN), program_id="other")
    path = tmp_path / "demo" / "journal" / "actions.jsonl"
    path.parent.mkdir(parents=True)
    original = (json.dumps(_action_to_record(action)) + "\n").encode()
    path.write_bytes(original)
    with pytest.raises(ValueError, match="Action program does not match selected program"):
        if operation == "load":
            load_actions("demo", programs_root=tmp_path)
        elif operation == "log":
            read_action_log("demo", programs_root=tmp_path)
        elif operation == "status":
            update_action_status("demo", action.id, ActionStatus.DONE, "Completed", updated_by="operator", programs_root=tmp_path)
        else:
            associate_action_with_work_item("demo", action.id, 2002, programs_root=tmp_path)
    assert path.read_bytes() == original
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == ["demo/journal/actions.jsonl"]


def test_load_actions_returns_empty_tuple_when_file_absent(tmp_path: Path) -> None:
    assert load_actions("demo", programs_root=tmp_path) == ()


def test_append_action_round_trips_and_dedupes_duplicate_ids(tmp_path: Path) -> None:
    action = _sample_action()

    append_action("demo", action, programs_root=tmp_path)
    append_action("demo", action, programs_root=tmp_path)

    loaded = load_actions("demo", programs_root=tmp_path)
    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=tmp_path)

    assert loaded == (action,)
    assert len(project_action_items(snapshot)) == 1
    assert project_action_items(snapshot)[0].id == action.id


def test_update_action_status_applies_latest_status_and_resolution_note(tmp_path: Path) -> None:
    action = _sample_action(status=ActionStatus.OPEN)
    append_action("demo", action, programs_root=tmp_path)

    update_action_status(
        "demo",
        action.id,
        ActionStatus.DONE,
        "Owner confirmed completion.",
        updated_by="demo",
        updated_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )

    loaded = load_actions("demo", programs_root=tmp_path)
    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=tmp_path)

    assert loaded[0].status is ActionStatus.DONE
    assert loaded[0].resolved_at == datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    assert loaded[0].resolution_note == "Owner confirmed completion."
    assert project_action_items(snapshot)[0].status is ActionStatus.DONE
    assert project_action_items(snapshot)[0].resolution_note == "Owner confirmed completion."


def test_append_action_threads_source_signal_id_onto_the_fact_revision(tmp_path: Path) -> None:
    # ADF-W2.4/W2.5: a signal-derived action's source_signal_id must land on
    # the ProgramFactRevision's own top-level source_signal_ids field (what
    # fact_lineage_coverage.py's classifier actually inspects), not just
    # inside the payload dict the ActionItem projection round-trips through.
    action = _sample_action()
    append_action("demo", action, programs_root=tmp_path)

    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=tmp_path)
    action_facts = [fact for fact in snapshot.facts if fact.fact_type == "action.item"]

    assert len(action_facts) == 1
    assert action_facts[0].source_signal_ids == ("signal-1",)
    assert has_fact_provenance(action_facts[0]) is True


def test_append_action_records_trace_link_when_correlation_id_present(tmp_path: Path) -> None:
    # ADF-W2.12: a gather-cycle correlation id threaded down to append_action
    # must produce a real stage="fact" OperationTrace link, no-op when absent
    # (the pre-existing default, so no other call site's behavior changes).
    action = _sample_action()
    append_action("demo", action, programs_root=tmp_path, correlation_id="gather-corr-1")

    trace = load_operation_trace("demo", "gather-corr-1", programs_root=tmp_path)
    assert trace is not None
    assert len(trace.fact_refs) == 1
    assert f"action.item:{action.id}" in trace.fact_refs[0]


def test_associate_action_with_work_item_updates_fact_store_projection(tmp_path: Path) -> None:
    action = _sample_action()
    append_action("demo", action, programs_root=tmp_path)

    associate_action_with_work_item("demo", action.id, 2002, programs_root=tmp_path)

    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=tmp_path)

    assert project_action_items(snapshot)[0].linked_work_item_ids == (1001, 2002)


def test_association_preserves_existing_status_updates(tmp_path: Path) -> None:
    action = _sample_action(status=ActionStatus.OPEN)
    append_action("demo", action, programs_root=tmp_path)
    update_action_status("demo", action.id, ActionStatus.DONE, "Owner confirmed completion.", updated_by="owner", programs_root=tmp_path)

    associate_action_with_work_item("demo", action.id, 2002, programs_root=tmp_path)

    loaded = load_actions("demo", programs_root=tmp_path)
    assert loaded[0].status is ActionStatus.DONE
    assert loaded[0].resolution_note == "Owner confirmed completion."
    assert loaded[0].linked_work_item_ids == (1001, 2002)


def test_assess_action_staleness_flags_only_overdue_active_actions() -> None:
    overdue_open = _sample_action(due_date=date(2026, 5, 1), status=ActionStatus.OPEN)
    overdue_in_progress = _sample_action(
        action_id="action-2",
        due_date=date(2026, 5, 2),
        status=ActionStatus.IN_PROGRESS,
    )
    overdue_proposed = _sample_action(
        action_id="action-3",
        due_date=date(2026, 5, 3),
        status=ActionStatus.PROPOSED,
    )
    completed = _sample_action(
        action_id="action-4",
        due_date=date(2026, 5, 4),
        status=ActionStatus.DONE,
    )

    overdue = assess_action_staleness(
        (overdue_open, overdue_in_progress, overdue_proposed, completed),
        as_of=date(2026, 5, 10),
    )

    assert overdue == (overdue_open, overdue_in_progress)


def test_build_action_id_is_stable() -> None:
    first = build_action_id(
        "demo",
        text="Follow up with the firmware team",
        owner_alias="owner",
        due_date=date(2026, 5, 12),
        source_signal_id="signal-1",
        workstream_id="acme",
        linked_work_item_ids=(1001,),
    )
    second = build_action_id(
        "demo",
        text=" Follow up with the firmware team ",
        owner_alias="owner",
        due_date=date(2026, 5, 12),
        source_signal_id="signal-1",
        workstream_id="acme",
        linked_work_item_ids=(1001,),
    )

    assert first == second


def test_match_action_to_ado_update_detects_later_trajectory_change() -> None:
    action = _sample_action(created_at=datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc))
    trajectories = {
        1001: (
            TrajectoryPoint(
                date=date(2026, 5, 7),
                state="Active",
                assigned_to="owner@example.com",
                target_date=None,
                risk_level=None,
                area_path="One\\Demo",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 9),
                state="Resolved",
                assigned_to="owner@example.com",
                target_date=None,
                risk_level=None,
                area_path="One\\Demo",
            ),
        )
    }

    assert match_action_to_ado_update(action, trajectories) is True


def test_update_action_status_raises_for_unknown_action(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown action"):
        update_action_status(
            "demo",
            "missing",
            ActionStatus.DONE,
            "done",
            updated_by="demo",
            programs_root=tmp_path,
        )


def test_load_actions_rejects_numeric_string_linked_work_item_ids(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":["1001"],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="linked_work_item_ids must contain integers only"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_list_linked_work_item_ids(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":"1001","linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="linked_work_item_ids must be a list of integers"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_action_id(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":123,"program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="id must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_created_at(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":123,"resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="created_at must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_naive_created_at(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="created_at must include timezone information"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_due_date(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":20260512,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="date field must be a string when provided"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_workstream_id(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":999,"created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="workstream_id must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_resolution_note(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"done","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":"2026-05-10T12:00:00+00:00","resolution_note":123}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="resolution_note must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_naive_resolved_at(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"done","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":"2026-05-10T12:00:00","resolution_note":"done"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="datetime field must include timezone information"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_source_signal_id(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":123,"source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="source_signal_id must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_non_string_status_update_note(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"open","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n'
        '{"record_type":"status_update","action_id":"action-1","new_status":"done","updated_at":"2026-05-10T12:00:00+00:00","updated_by":"owner","note":123}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="note must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_naive_status_update_updated_at(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"action","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"open","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n'
        '{"record_type":"status_update","action_id":"action-1","new_status":"done","updated_at":"2026-05-10T12:00:00","updated_by":"owner","note":"done"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="updated_at must include timezone information"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_quarantines_non_object_rows(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text('["not","an","object"]\n', encoding="utf-8")

    result = load_actions("demo", programs_root=tmp_path)
    assert result == ()
    quarantine_dir = actions_path.parent / "quarantine"
    assert quarantine_dir.exists()
    quarantined = tuple(quarantine_dir.glob("actions.*.jsonl"))
    assert len(quarantined) == 1


def test_load_actions_rejects_non_string_record_type(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":123,"id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="record_type must be a string"):
        load_actions("demo", programs_root=tmp_path)


def test_load_actions_rejects_unknown_record_type(tmp_path: Path) -> None:
    actions_path = tmp_path / "demo" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True)
    actions_path.write_text(
        '{"record_type":"bogus","id":"action-1","program_id":"demo","text":"Follow up","owner_alias":"owner","due_date":null,"status":"proposed","source_signal_id":"signal-1","source_type":"signal","linked_work_item_ids":[1001],"linked_claim_id":null,"linked_risk_id":null,"workstream_id":"acme","created_at":"2026-05-08T08:00:00+00:00","resolved_at":null,"resolution_note":null}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown action log record_type 'bogus'"):
        load_actions("demo", programs_root=tmp_path)


def _sample_action(
    *,
    action_id: str = "action-1",
    due_date: date | None = date(2026, 5, 12),
    status: ActionStatus = ActionStatus.PROPOSED,
    created_at: datetime = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc),
) -> ActionItem:
    return ActionItem(
        id=action_id,
        program_id="demo",
        text="Follow up with the firmware team",
        owner_alias="owner",
        due_date=due_date,
        status=status,
        source_signal_id="signal-1",
        source_type=ActionSourceType.SIGNAL,
        linked_work_item_ids=(1001,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id="acme",
        created_at=created_at,
        resolved_at=None,
        resolution_note=None,
    )