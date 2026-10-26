from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
import typer.core
from typer.testing import CliRunner

from src.core.ncfl_proposal_store import (
    conflicting_pending_proposals,
    get_proposals_path,
    load_proposals,
    save_proposals,
    stage_extracted_proposals,
    update_proposal_status,
)
from tests.unit.test_context_gap_reply_import import _bound_intake_workspace
from tests.unit.test_ncfl_flow import _proposal


@pytest.fixture(autouse=True)
def _plain_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typer.core, "HAS_RICH", False)


def _files(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _queue(programs: Path, payload: object) -> Path:
    path = get_proposals_path("sample", 1, programs_root=programs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _pending():
    return replace(
        _proposal("retained", issue_number=1, conflict_key="old", source_value="old"),
        program_id="sample", edition_id="sample_weekly",
    )


@pytest.mark.parametrize("payload", [None, {}, {"retained": "data"}, False, 7, "retained", [None], [7], ["retained"], [False]])
@pytest.mark.parametrize("operation", ["load", "stage", "dismiss"])
def test_corrupt_queue_rejects_without_discarding_original_bytes(
    tmp_path: Path, payload: object, operation: str,
) -> None:
    from cli import app

    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    _queue(programs, payload)
    before = _files(tmp_path)
    if operation == "load":
        with pytest.raises(ValueError, match="Invalid NCFL proposal queue"):
            load_proposals("sample", issue_number=1, programs_root=programs)
    elif operation == "stage":
        with pytest.raises(ValueError, match="Invalid NCFL proposal queue"):
            stage_extracted_proposals("sample", 1, (_pending(),), programs_root=programs)
    else:
        result = CliRunner().invoke(app, [
            "context", "dismiss", "--edition", "sample_weekly", "--issue", "1",
            "--proposal-id", "retained", "--reason", "synthetic reason",
            "--programs-root", str(programs),
        ])
        assert result.exit_code == 2, result.output + repr(result.exception)
        assert "Invalid NCFL proposal queue" in result.output
        assert "Unknown proposal_id" not in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("operation", ["pending", "status", "dismiss", "stage"])
def test_mixed_queue_cannot_filter_or_overwrite_unreadable_retained_entry(
    tmp_path: Path, operation: str,
) -> None:
    from cli import app

    programs, _, _ = _bound_intake_workspace(tmp_path)
    _queue(programs, [_pending().to_json(), None])
    before = _files(tmp_path)
    if operation == "stage":
        new = replace(_pending(), proposal_id="new", conflict_key="new")
        with pytest.raises(ValueError, match="Invalid NCFL proposal queue"):
            stage_extracted_proposals("sample", 1, (new,), programs_root=programs)
    else:
        args = ["context", "dismiss"] if operation == "dismiss" else ["context", "proposals"]
        args += ["--edition", "sample_weekly", "--issue", "1", "--programs-root", str(programs)]
        args += (
            ["--proposal-id", "retained", "--reason", "synthetic reason"]
            if operation == "dismiss" else ["--status", "pending" if operation == "pending" else "accepted", "--format", "json"]
        )
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, result.output + repr(result.exception)
        expected = (
            "Invalid NCFL proposal queue" if operation == "dismiss"
            else "Context proposal queue is invalid; repair it before retrying."
        )
        assert expected in result.output
        assert not isinstance(result.exception, ValueError)
        assert "Traceback" not in result.output
        assert '"proposal_count": 0' not in result.output
        assert "Dismissed proposal" not in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("payload", [
    {"retained": "synthetic-corrupt-content"}, None, ["synthetic-corrupt-content"],
])
@pytest.mark.parametrize("operation", ["pending", "status", "manual-diff", "apply-batch"])
def test_installed_queue_consumers_report_safe_cli_error_without_writes(
    tmp_path: Path, payload: object, operation: str,
) -> None:
    from cli import app

    programs, _, _ = _bound_intake_workspace(tmp_path)
    _queue(programs, payload)
    before = _files(tmp_path)
    command = "proposals" if operation in {"pending", "status"} else operation
    args = [
        "context", command, "--edition", "sample_weekly", "--issue", "1",
        "--programs-root", str(programs),
    ]
    if operation in {"pending", "status"}:
        args += ["--status", "pending" if operation == "pending" else "accepted", "--format", "json"]
    elif operation == "manual-diff":
        args += ["--proposal-id", "retained"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert isinstance(result.exception, SystemExit)
    assert "Context proposal queue is invalid; repair it before retrying." in result.output
    assert "synthetic-corrupt-content" not in result.output
    assert "Invalid NCFL proposal queue" not in result.output
    assert "ValueError" not in result.output and "Traceback" not in result.output
    assert '"proposal_count": 0' not in result.output
    assert "No accepted proposals" not in result.output
    assert "--- " not in result.output and "+++ " not in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("payload", [{}, None, [None]])
def test_actual_bound_reply_intake_rejects_corrupt_existing_queue_without_writes(
    tmp_path: Path, payload: object,
) -> None:
    from cli import app

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    _queue(programs, payload)
    before = _files(tmp_path)
    result = CliRunner().invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Reply intake unavailable" in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("initial", ["absent", "empty"])
def test_absent_and_empty_legacy_queue_still_support_actual_pending_manual_intake(
    tmp_path: Path, initial: str,
) -> None:
    from cli import app

    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    if initial == "empty":
        _queue(programs, [])
    assert load_proposals("sample", programs_root=programs) == ()
    result = CliRunner().invoke(app, [
        "context", "import-reply", "--edition", "sample_weekly", "--issue", "1",
        "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
        "--solicitation-id", packet.id, "--programs-root", str(programs),
    ])
    assert result.exit_code == 0, result.output + repr(result.exception)
    proposal, = load_proposals("sample", programs_root=programs)
    assert proposal.status == "pending"
    assert not proposal.batch_eligible and not proposal.decision_history
    assert proposal.current_value_hash == packet.current_value_hash


def test_valid_queue_round_trip_and_status_filter_remain_compatible(tmp_path: Path) -> None:
    pending = _pending()
    accepted = replace(pending, proposal_id="accepted", conflict_key="other", status="accepted")
    save_proposals("sample", 1, (pending, accepted), programs_root=tmp_path)
    assert load_proposals("sample", status_filter={"pending"}, programs_root=tmp_path) == (pending,)
    assert load_proposals("sample", status_filter={"accepted"}, programs_root=tmp_path) == (accepted,)


@pytest.mark.parametrize("corruption", [
    "empty", "missing-id", "missing-issue", "issue-null", "issue-list",
    "issue-object", "issue-text", "datetime", "history-type",
])
@pytest.mark.parametrize("operation", ["load", "stage", "proposals", "manual-diff", "apply-batch"])
def test_invalid_record_parser_errors_are_safe_and_nonmutating(
    tmp_path: Path, corruption: str, operation: str,
) -> None:
    from cli import app

    programs, _, _ = _bound_intake_workspace(tmp_path)
    raw = _pending().to_json()
    if corruption == "empty":
        raw = {}
    elif corruption in {"missing-id", "missing-issue"}:
        del raw["proposal_id" if corruption == "missing-id" else "issue_number"]
    elif corruption.startswith("issue-"):
        raw["issue_number"] = {
            "issue-null": None, "issue-list": [], "issue-object": {},
            "issue-text": "synthetic-corrupt-content",
        }[corruption]
    elif corruption == "datetime":
        raw["extracted_at"] = "synthetic-corrupt-content"
    else:
        raw["decision_history"] = 7
    _queue(programs, [_pending().to_json(), raw])
    before = _files(tmp_path)
    if operation in {"load", "stage"}:
        with pytest.raises(ValueError) as caught:
            if operation == "load":
                load_proposals("sample", status_filter={"accepted"}, programs_root=programs)
            else:
                new = replace(_pending(), proposal_id="new", conflict_key="new")
                stage_extracted_proposals("sample", 1, (new,), programs_root=programs)
        assert str(caught.value) == "Context proposal queue contains an invalid record."
    else:
        args = [
            "context", operation, "--edition", "sample_weekly", "--issue", "1",
            "--programs-root", str(programs),
        ]
        if operation == "proposals":
            args += ["--status", "accepted", "--format", "json"]
        elif operation == "manual-diff":
            args += ["--proposal-id", "retained"]
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, result.output + repr(result.exception)
        assert isinstance(result.exception, SystemExit)
        assert "Context proposal queue is invalid; repair it before retrying." in result.output
        assert "synthetic-corrupt-content" not in result.output
        assert "Traceback" not in result.output and "ValueError" not in result.output
        assert '"proposal_count": 0' not in result.output
        assert "No accepted proposals" not in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("damaged_payload", [{}, [None], [{}]])
@pytest.mark.parametrize("operation", ["stage", "dismiss"])
def test_staging_rejects_damaged_other_issue_before_target_or_index_write(
    tmp_path: Path, damaged_payload: object, operation: str,
) -> None:
    programs = tmp_path / "programs"
    retained = _pending()
    save_proposals("sample", 1, (retained,), programs_root=programs)
    other = get_proposals_path("sample", 2, programs_root=programs)
    other.write_text(json.dumps(damaged_payload) + "\n", encoding="utf-8")
    before = _files(tmp_path)
    new = replace(retained, proposal_id="new", conflict_key="new")

    with pytest.raises(ValueError):
        if operation == "stage":
            stage_extracted_proposals("sample", 1, (new,), programs_root=programs)
        else:
            update_proposal_status(
                "sample", proposal_id=retained.proposal_id, new_status="dismissed",
                actor="tester", issue_number=1, rationale="synthetic reason",
                programs_root=programs,
            )

    assert _files(tmp_path) == before


@pytest.mark.parametrize("cache", [
    "missing", "invalid-json", "nonobject", "missing-conflicts", "wrong-conflicts",
    "empty", "terminal", "nonlist", "nonobject-entry",
])
@pytest.mark.parametrize("consumer", ["core", "json", "human"])
def test_authoritative_pending_conflicts_survive_missing_corrupt_or_stale_cache(
    tmp_path: Path, cache: str, consumer: str,
) -> None:
    from cli import app

    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    first = _pending()
    second = replace(first, proposal_id="second", issue_number=2)
    terminal = replace(first, proposal_id="terminal", status="dismissed")
    save_proposals("sample", 1, (first, terminal), programs_root=programs)
    save_proposals("sample", 2, (second,), programs_root=programs)
    index = programs / "sample" / "context_proposals" / "_conflict_index.json"
    original = json.loads(index.read_text(encoding="utf-8"))
    if cache == "missing":
        index.unlink()
    elif cache == "invalid-json":
        index.write_text("{synthetic-corrupt-content\n", encoding="utf-8")
    else:
        values = {
            "nonobject": [], "missing-conflicts": {}, "wrong-conflicts": {"conflicts": []},
            "empty": {"conflicts": {}},
            "terminal": {"conflicts": {"old": [
                {**entry, "status": "dismissed"} for entry in original["conflicts"]["old"]
            ]}},
            "nonlist": {"conflicts": {"old": {}}},
            "nonobject-entry": {"conflicts": {"old": [None]}},
        }
        index.write_text(json.dumps(values[cache]), encoding="utf-8")
    before = _files(tmp_path)
    if consumer == "core":
        conflicts = conflicting_pending_proposals("sample", programs_root=programs)
        assert set(conflicts) == {"old"}
        assert {entry["proposal_id"] for entry in conflicts["old"]} == {"retained", "second"}
    else:
        result = CliRunner().invoke(app, [
            "context", "proposals", "--edition", "sample_weekly", "--issue", "1",
            "--status", "pending", "--format", consumer, "--programs-root", str(programs),
        ])
        assert result.exit_code == 0, result.output + repr(result.exception)
        if consumer == "json":
            payload = json.loads(result.output)
            assert payload["proposal_count"] == 1
            assert set(payload["conflicts"]) == {"old"}
            assert {entry["proposal_id"] for entry in payload["conflicts"]["old"]} == {"retained", "second"}
        else:
            assert "Cross-issue conflicts: 1" in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("consumer", ["core", "json", "human"])
def test_stale_cache_cannot_fabricate_pending_conflicts(tmp_path: Path, consumer: str) -> None:
    from cli import app

    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    first = _pending()
    second = replace(first, proposal_id="second", issue_number=2)
    save_proposals("sample", 1, (first,), programs_root=programs)
    save_proposals("sample", 2, (second,), programs_root=programs)
    other = get_proposals_path("sample", 2, programs_root=programs)
    other.write_text(json.dumps([replace(second, status="accepted").to_json()]), encoding="utf-8")
    before = _files(tmp_path)
    if consumer == "core":
        assert conflicting_pending_proposals("sample", programs_root=programs) == {}
    else:
        result = CliRunner().invoke(app, [
            "context", "proposals", "--edition", "sample_weekly", "--format", consumer,
            "--programs-root", str(programs),
        ])
        assert result.exit_code == 0, result.output + repr(result.exception)
        if consumer == "json":
            assert json.loads(result.output)["conflicts"] == {}
        else:
            assert "Cross-issue conflicts:" not in result.output
    assert _files(tmp_path) == before


@pytest.mark.parametrize("consumer", ["core", "json", "human"])
def test_conflict_inspection_rejects_damaged_unselected_authoritative_queue(
    tmp_path: Path, consumer: str,
) -> None:
    from cli import app

    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    save_proposals("sample", 1, (_pending(),), programs_root=programs)
    get_proposals_path("sample", 2, programs_root=programs).write_text(
        '{"synthetic-corrupt-content": true}\n', encoding="utf-8",
    )
    before = _files(tmp_path)
    if consumer == "core":
        with pytest.raises(ValueError):
            conflicting_pending_proposals("sample", programs_root=programs)
    else:
        result = CliRunner().invoke(app, [
            "context", "proposals", "--edition", "sample_weekly", "--issue", "1",
            "--format", consumer, "--programs-root", str(programs),
        ])
        assert result.exit_code == 2, result.output + repr(result.exception)
        assert isinstance(result.exception, SystemExit)
        assert "Context proposal queue is invalid; repair it before retrying." in result.output
        assert "synthetic-corrupt-content" not in result.output
        assert "Traceback" not in result.output and "ValueError" not in result.output
    assert _files(tmp_path) == before
