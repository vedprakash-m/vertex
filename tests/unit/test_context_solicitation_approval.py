from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.test_commands_context_solicitation import _args, _files, _workspace


def _packet(root: Path) -> tuple[Path, Path]:
    from cli import app

    programs, fingerprint = _workspace(root)
    result = CliRunner().invoke(app, _args(programs, fingerprint))
    assert result.exit_code == 0, result.output + repr(result.exception)
    return programs, next((programs / "sample" / "nudge" / "drafts").glob("*.eml"))


def _nudge(programs: Path, mode: str, draft: Path):
    from cli import app

    return CliRunner().invoke(app, [
        "nudge", "--program", "sample", mode, draft.name, "--programs-root", str(programs),
    ])


@pytest.mark.parametrize("change", ["absent", "filename", "changed", "stale", "missing-log", "foreign"])
def test_bound_packet_cannot_be_attested_without_exact_current_approval(tmp_path: Path, change: str) -> None:
    programs, draft = _packet(tmp_path)
    nudge = draft.parent.parent
    if change not in {"absent", "filename"}:
        approved = _nudge(programs, "--approve-draft", draft)
        assert approved.exit_code == 0, approved.output + repr(approved.exception)
    if change == "filename":
        (nudge / "draft_approvals.json").write_text(json.dumps([{
            "filename": draft.name, "content_hash": "unrelated", "program_id": "sample",
        }]))
    elif change == "changed":
        draft.write_bytes(draft.read_bytes() + b"\r\nchanged")
    elif change == "stale":
        (programs / "sample" / "workstream_registry.yaml").write_text(
            "workstreams:\n  - id: deployment\n    deep_context:\n      why: Changed\n",
        )
    elif change == "missing-log":
        (programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl").unlink()
    elif change == "foreign":
        index = nudge / "draft_approvals.json"
        records = json.loads(index.read_text())
        records[0]["program_id"] = "other"
        index.write_text(json.dumps(records))
    before = _files(tmp_path)
    result = _nudge(programs, "--mark-sent", draft)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("change", ["changed", "stale", "foreign", "duplicate", "invalid-registry"])
def test_bound_packet_approval_rejects_changed_or_ambiguous_binding(tmp_path: Path, change: str) -> None:
    programs, draft = _packet(tmp_path)
    log = programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl"
    if change == "changed":
        draft.write_bytes(draft.read_bytes() + b"\r\nchanged")
    elif change in {"stale", "invalid-registry"}:
        (programs / "sample" / "workstream_registry.yaml").write_text(
            "workstreams: [" if change == "invalid-registry"
            else "workstreams:\n  - id: deployment\n    deep_context:\n      why: Changed\n",
        )
    else:
        record = json.loads(log.read_text())
        if change == "foreign":
            record["program_id"] = "other"
        log.write_text(json.dumps(record) + "\n" + (json.dumps(record) + "\n" if change == "duplicate" else ""))
    before = _files(tmp_path)
    result = _nudge(programs, "--approve-draft", draft)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


def test_real_selected_root_packet_approval_and_manual_attestation(tmp_path: Path) -> None:
    programs, draft = _packet(tmp_path)
    registry = programs / "sample" / "workstream_registry.yaml"
    before = registry.read_bytes()
    approved = _nudge(programs, "--approve-draft", draft)
    assert approved.exit_code == 0, approved.output + repr(approved.exception)
    index = draft.parent.parent / "draft_approvals.json"
    entry = json.loads(index.read_text())[0]
    assert entry["content_hash"] == hashlib.sha256(draft.read_bytes()).hexdigest()
    assert entry["program_id"] == "sample"
    sent = _nudge(programs, "--mark-sent", draft)
    assert sent.exit_code == 0, sent.output + repr(sent.exception)
    assert (draft.parent.parent / "published_eml" / draft.name).read_bytes() == draft.read_bytes()
    assert registry.read_bytes() == before
    assert not (programs / "sample" / "context_proposals").exists()


def test_packet_cannot_be_retargeted_by_changing_log_metadata(tmp_path: Path) -> None:
    programs, draft = _packet(tmp_path)
    registry = programs / "sample" / "workstream_registry.yaml"
    registry.write_text(registry.read_text() + "  - id: other\n")
    log = programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl"
    record = json.loads(log.read_text())
    record["target_key"] = "other"
    record["gap_fingerprint"] = record["gap_fingerprint"].replace(":deployment:", ":other:")
    log.write_text(json.dumps(record) + "\n")
    before = _files(tmp_path)
    result = _nudge(programs, "--approve-draft", draft)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


def test_ordinary_nudge_draft_ignores_unrelated_solicitation_log_corruption(tmp_path: Path) -> None:
    from cli import app

    programs, _ = _workspace(tmp_path)
    drafts = programs / "sample" / "nudge" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "ordinary.eml"
    draft.write_bytes(b"Subject: Ordinary\r\nTo: reviewer@example.com\r\n\r\nBody")
    (programs / "sample" / "_feedback" / "context_gap_solicitations.jsonl").write_text("{invalid\n")
    result = CliRunner().invoke(app, [
        "nudge", "--program", "sample", "--approve-draft", draft.name, "--programs-root", str(programs),
    ])
    assert result.exit_code == 0, result.output + repr(result.exception)


@pytest.mark.parametrize("mode", ["--approve-draft", "--mark-sent"])
def test_nudge_draft_reference_cannot_leave_selected_directory(tmp_path: Path, mode: str) -> None:
    from cli import app

    programs, _ = _workspace(tmp_path)
    before = _files(tmp_path)
    result = CliRunner().invoke(app, [
        "nudge", "--program", "sample", mode, "..\\outside.eml", "--programs-root", str(programs),
    ])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("target", ["database", "registry", "approval", "draft", "draft-fuzzy"])
def test_bound_packet_lifecycle_rejects_storage_link_escape(tmp_path: Path, target: str) -> None:
    programs, draft = _packet(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside-" + target)
    outside.mkdir()
    if target == "database":
        link, destination = programs.parent / "sample", outside
    elif target == "registry":
        link, destination = programs / "sample" / "workstream_registry.yaml", outside / "registry.yaml"
        link.rename(destination)
    elif target == "approval":
        link, destination = draft.parent.parent / "draft_approvals.json", outside / "approval.json"
        destination.write_text("[]")
    else:
        link, destination = draft, outside / draft.name
        draft.rename(destination)
    try:
        link.symlink_to(destination, target_is_directory=target == "database")
    except OSError as error:
        pytest.skip(f"Local symbolic links unavailable: {error}")
    before = _files(outside)
    result = _nudge(programs, "--approve-draft", Path(draft.stem[10:20]) if target == "draft-fuzzy" else draft)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert _files(outside) == before
