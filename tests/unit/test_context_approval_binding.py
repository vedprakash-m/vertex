from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
import typer.core
import yaml
from typer.testing import CliRunner

from src.core.ncfl_apply import _journal_path, _write_journal, apply_proposal
from src.core.ncfl_models import ContextUpdateProposal, DecisionRecord
from src.core.ncfl_proposal_store import (
    get_proposals_path,
    load_proposals,
    stage_extracted_proposals,
    update_proposal_status,
)
from src.core.operator_identity import OperatorIdentity
from tests.unit.test_ncfl_flow import _proposal


@pytest.fixture(autouse=True)
def _plain_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typer.core, "HAS_RICH", False)
    monkeypatch.setattr(
        "src.commands.context.capture_operator_identity",
        lambda actor: OperatorIdentity(actor=actor, principal="synthetic-principal", machine="m", session="s"),
    )


def _pending() -> ContextUpdateProposal:
    return replace(
        _proposal("reviewed", issue_number=1, conflict_key="assumptions:assume-1:text", source_value="reviewed value"),
        program_id="sample", edition_id="sample_weekly", target_store="assumptions",
        target_key="assume-1", target_field="text", current_value="baseline",
        current_value_hash=hashlib.sha256(b"baseline").hexdigest(),
    )


def _workspace(tmp_path: Path, *, accept: bool = True) -> tuple[Path, Path]:
    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    authored = programs / "sample" / "assumptions.yaml"
    authored.write_text(yaml.safe_dump({"schema_version": "1.0", "assumptions": [
        {"id": key, "text": "baseline", "status": "active", "identified_date": "2024-01-01"}
        for key in ("assume-1", "assume-2")
    ]}, sort_keys=False), encoding="utf-8")
    stage_extracted_proposals("sample", 1, (_pending(),), programs_root=programs)
    if accept:
        update_proposal_status(
            "sample", proposal_id="reviewed", new_status="accepted", actor="synthetic-reviewer",
            issue_number=1, programs_root=programs,
        )
    return programs, authored


def _edit_queue(programs: Path, edit) -> None:
    queue = get_proposals_path("sample", 1, programs_root=programs)
    records = json.loads(queue.read_text(encoding="utf-8"))
    edit(records[0])
    queue.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _apply(programs: Path, *extra: str):
    from cli import app

    return CliRunner().invoke(app, [
        "context", "apply", "--edition", "sample_weekly", "--issue", "1",
        "--proposal-id", "reviewed", "--programs-root", str(programs), *extra,
    ])


def _texts(authored: Path) -> dict[str, str]:
    return {item["id"]: item["text"] for item in yaml.safe_load(authored.read_text(encoding="utf-8"))["assumptions"]}


def test_unchanged_accepted_proposal_still_applies(tmp_path: Path) -> None:
    programs, authored = _workspace(tmp_path)

    result = _apply(programs)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert result.output.startswith("applied:")
    assert _texts(authored) == {"assume-1": "reviewed value", "assume-2": "baseline"}


@pytest.mark.parametrize("dry_run", [False, True])
def test_captured_acceptance_cannot_override_retained_revocation(
    tmp_path: Path, dry_run: bool,
) -> None:
    programs, authored = _workspace(tmp_path)
    captured = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    revoked = update_proposal_status(
        "sample", proposal_id="reviewed", new_status="pending", actor="reviewer",
        issue_number=1, programs_root=programs,
    )
    queue = get_proposals_path("sample", 1, programs_root=programs)
    before = (authored.read_bytes(), queue.read_bytes())

    result = apply_proposal(captured, actor="synthetic", programs_root=programs, dry_run=dry_run)

    assert result.action == "refused_unbound_approval"
    assert (authored.read_bytes(), queue.read_bytes()) == before
    assert load_proposals("sample", issue_number=1, programs_root=programs) == (revoked,)
    assert not _journal_path("sample", "reviewed", programs_root=programs).exists()


@pytest.mark.parametrize("queue_state", ["missing", "corrupt", "reapproved_revision"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_captured_acceptance_requires_available_matching_retained_revision(
    tmp_path: Path, queue_state: str, dry_run: bool,
) -> None:
    programs, authored = _workspace(tmp_path)
    captured = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    queue = get_proposals_path("sample", 1, programs_root=programs)
    if queue_state == "missing":
        queue.unlink()
    elif queue_state == "corrupt":
        queue.write_text("{broken", encoding="utf-8")
    else:
        _edit_queue(programs, lambda record: record.update({"source_value": "newly reviewed", "status": "pending"}))
        update_proposal_status(
            "sample", proposal_id="reviewed", new_status="accepted", actor="reviewer",
            issue_number=1, programs_root=programs,
        )
    before = (authored.read_bytes(), queue.read_bytes() if queue.exists() else None)

    result = apply_proposal(captured, actor="synthetic", programs_root=programs, dry_run=dry_run)

    assert result.action == ("needs_repair" if queue_state == "corrupt" else "refused_unbound_approval")
    assert (authored.read_bytes(), queue.read_bytes() if queue.exists() else None) == before
    assert not _journal_path("sample", "reviewed", programs_root=programs).exists()


@pytest.mark.parametrize("with_current_hash", [True, False])
def test_completed_apply_replay_changes_no_authored_or_audit_bytes(
    tmp_path: Path, with_current_hash: bool,
) -> None:
    programs, authored = _workspace(tmp_path, accept=False)
    if not with_current_hash:
        _edit_queue(programs, lambda record: record.__setitem__("current_value_hash", None))
    update_proposal_status(
        "sample", proposal_id="reviewed", new_status="accepted", actor="reviewer",
        issue_number=1, programs_root=programs,
    )
    first = _apply(programs)
    assert first.exit_code == 0, first.output
    queue = get_proposals_path("sample", 1, programs_root=programs)
    changelog = programs / "sample" / "_ncfl_apply" / "changelog.jsonl"
    before = (authored.read_bytes(), queue.read_bytes(), changelog.read_bytes())

    second = _apply(programs)

    assert second.exit_code == 0, second.output
    assert second.output.startswith("skipped_already_applied:")
    assert (authored.read_bytes(), queue.read_bytes(), changelog.read_bytes()) == before


@pytest.mark.parametrize("target_state", ["deleted_file", "deleted_record", "malformed", "wrong_shape"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_expected_current_hash_cannot_match_unavailable_target(
    tmp_path: Path, target_state: str, dry_run: bool,
) -> None:
    programs, authored = _workspace(tmp_path)
    if target_state == "deleted_file":
        authored.unlink()
    elif target_state == "deleted_record":
        document = yaml.safe_load(authored.read_text(encoding="utf-8"))
        document["assumptions"] = document["assumptions"][1:]
        authored.write_text(yaml.safe_dump(document), encoding="utf-8")
    else:
        authored.write_text("text: [" if target_state == "malformed" else "[]", encoding="utf-8")
    before = authored.read_bytes() if authored.exists() else None
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]

    result = apply_proposal(proposal, actor="synthetic", programs_root=programs, dry_run=dry_run)

    assert result.action == "skipped_stale"
    assert "unavailable" in result.note
    assert (authored.read_bytes() if authored.exists() else None) == before
    assert not _journal_path("sample", "reviewed", programs_root=programs).exists()


@pytest.mark.parametrize("current", ["baseline", "changed", None])
def test_knowledge_current_hash_reads_actual_document_before_apply(
    tmp_path: Path, current: str | None,
) -> None:
    programs, _ = _workspace(tmp_path, accept=False)
    document = programs / "sample" / "knowledge" / "context.md"
    document.parent.mkdir()
    if current is not None:
        document.write_text(current, encoding="utf-8")
    _edit_queue(programs, lambda record: record.update({
        "target_store": "knowledge_doc", "target_key": "context.md", "target_field": "body",
        "conflict_key": "knowledge_doc:context.md:body",
    }))
    accepted = update_proposal_status(
        "sample", proposal_id="reviewed", new_status="accepted", actor="reviewer",
        issue_number=1, programs_root=programs,
    )

    result = apply_proposal(accepted, actor="synthetic", programs_root=programs)

    if current == "baseline":
        assert result.action == "applied"
        assert document.read_text(encoding="utf-8") == "reviewed value"
    else:
        assert result.action == "skipped_stale"
        assert (document.read_text(encoding="utf-8") if document.exists() else None) == current
        assert not _journal_path("sample", "reviewed", programs_root=programs).exists()


def test_knowledge_current_read_preserves_writer_field_validation(tmp_path: Path) -> None:
    programs, _ = _workspace(tmp_path, accept=False)
    document = programs / "sample" / "knowledge" / "context.md"
    document.parent.mkdir()
    document.write_text("baseline", encoding="utf-8")
    _edit_queue(programs, lambda record: record.update({
        "target_store": "knowledge_doc", "target_key": "context.md", "target_field": "summary",
        "conflict_key": "knowledge_doc:context.md:summary",
    }))
    accepted = update_proposal_status(
        "sample", proposal_id="reviewed", new_status="accepted", actor="reviewer",
        issue_number=1, programs_root=programs,
    )
    result = apply_proposal(accepted, actor="synthetic", programs_root=programs)
    assert result.action == "needs_repair"
    assert "Unknown knowledge_doc target_field" in result.note
    assert document.read_text(encoding="utf-8") == "baseline"
    assert tuple(document.parent.glob("*.bak")) == ()


@pytest.mark.parametrize("payload", [
    "{broken", "null", "[]", "{}", '{"state":"applied","proposal_id":"other"}',
    '{"state":"unknown","proposal_id":"reviewed"}',
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_retained_journal_requires_repair_without_writes(
    tmp_path: Path, payload: str, dry_run: bool,
) -> None:
    programs, authored = _workspace(tmp_path)
    journal = _journal_path("sample", "reviewed", programs_root=programs)
    journal.parent.mkdir(parents=True)
    journal.write_text(payload, encoding="utf-8")
    before = (journal.read_bytes(), authored.read_bytes())

    result = _apply(programs, *(["--dry-run"] if dry_run else []))

    assert result.exit_code == 3
    assert "needs_repair:" in result.output
    assert (journal.read_bytes(), authored.read_bytes()) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_terminal_repair_cannot_be_replayed_by_ordinary_retry(tmp_path: Path, dry_run: bool) -> None:
    programs, authored = _workspace(tmp_path)
    journal = _journal_path("sample", "reviewed", programs_root=programs)
    _write_journal(journal, "reviewed", "needs_repair", note="uncertain partial write")
    before = (journal.read_bytes(), authored.read_bytes())
    result = _apply(programs, *(["--dry-run"] if dry_run else []))
    assert result.exit_code == 3
    assert "needs_repair:" in result.output
    assert "reconcil" in result.output.lower()
    assert (journal.read_bytes(), authored.read_bytes()) == before


def test_acceptance_records_digest_only_on_acceptance_transition(tmp_path: Path) -> None:
    programs, _ = _workspace(tmp_path)
    accepted = load_proposals("sample", issue_number=1, programs_root=programs)[0]

    acceptance = accepted.decision_history[-1]
    assert (acceptance.from_status, acceptance.to_status) == ("pending", "accepted")
    assert acceptance.payload_digest == _pending().approval_payload_digest()
    assert accepted.approval_payload_digest() == _pending().approval_payload_digest()

    reapplied = update_proposal_status(
        "sample", proposal_id="reviewed", new_status="accepted", actor="applier",
        issue_number=1, programs_root=programs,
    )
    assert reapplied.decision_history[-1].payload_digest is None
    assert "payload_digest" not in reapplied.decision_history[-1].to_json()


def test_digest_covers_material_fields_and_excludes_decision_metadata() -> None:
    base = _pending()
    for field, value in {
        "proposal_id": "other", "program_id": "other", "issue_number": 2, "edition_id": "other",
        "source_type": "other", "extractor_version": "9.9.9", "source_artifact": "other",
        "source_field": "other", "extraction_method": "other", "target_store": "decisions",
        "target_key": "other", "target_field": "other", "source_value": "other",
        "current_value": "other", "current_value_hash": "other", "confidence": "low",
        "batch_eligible": False, "extraction_method_rationale": "other", "conflict_key": "other",
    }.items():
        assert replace(base, **{field: value}).approval_payload_digest() != base.approval_payload_digest(), field
    decided = replace(
        base, status="accepted", superseded_by="x", rationale="x", applied_by="x", dismissed_by="x",
        decision_history=(DecisionRecord(base.extracted_at, "x", "pending", "accepted", None, "d"),),
    )
    assert decided.approval_payload_digest() == base.approval_payload_digest()


@pytest.mark.parametrize(("field", "changed"), [
    ("source_value", "unreviewed value"),
    ("target_key", "assume-2"),
    ("current_value_hash", hashlib.sha256(b"baseline").hexdigest().upper()),
    ("source_artifact", "overrides/issue_099.yaml"),
])
def test_content_changed_after_acceptance_cannot_reach_authored_apply(
    tmp_path: Path, field: str, changed: str,
) -> None:
    programs, authored = _workspace(tmp_path)
    _edit_queue(programs, lambda record: record.__setitem__(field, changed))
    before = authored.read_bytes()

    result = _apply(programs)

    assert authored.read_bytes() == before, result.output
    assert result.exit_code == 1
    assert "applied:" not in result.output
    assert "refused_unbound_approval: proposal payload changed after acceptance" in result.output
    assert "re-review" in result.output
    assert not _journal_path("sample", "reviewed", programs_root=programs).exists()


def test_dry_run_also_refuses_changed_payload(tmp_path: Path) -> None:
    programs, authored = _workspace(tmp_path)
    _edit_queue(programs, lambda record: record.__setitem__("source_value", "unreviewed value"))
    before = authored.read_bytes()

    result = _apply(programs, "--dry-run")

    assert result.exit_code == 1
    assert "refused_unbound_approval" in result.output
    assert "would apply" not in result.output
    assert authored.read_bytes() == before


@pytest.mark.parametrize("history", ["missing", "hashless", "forged_reapply"])
def test_unbound_historical_acceptance_refuses_without_backfill(tmp_path: Path, history: str) -> None:
    programs, authored = _workspace(tmp_path, accept=False)

    def make_unbound(record: dict) -> None:
        record["status"] = "accepted"
        if history == "hashless":
            record["decision_history"] = [{
                "timestamp": "2026-01-01T00:00:00Z", "actor": "legacy", "from_status": "pending",
                "to_status": "accepted", "note": None,
            }]
        elif history == "forged_reapply":
            record["decision_history"] = [{
                "timestamp": "2026-01-01T00:00:00Z", "actor": "legacy", "from_status": "accepted",
                "to_status": "accepted", "note": None,
                "payload_digest": _pending().approval_payload_digest(),
            }]

    _edit_queue(programs, make_unbound)
    queue = get_proposals_path("sample", 1, programs_root=programs)
    queue_before, authored_before = queue.read_bytes(), authored.read_bytes()

    result = _apply(programs)

    assert result.exit_code == 1
    assert "refused_unbound_approval: acceptance has no payload binding" in result.output
    assert "re-review the current proposal" in result.output
    assert authored.read_bytes() == authored_before
    assert queue.read_bytes() == queue_before


@pytest.mark.parametrize("dry_run", [False, True])
def test_batch_refusal_returns_failure_without_authored_writes(tmp_path: Path, dry_run: bool) -> None:
    from cli import app

    programs, authored = _workspace(tmp_path)
    _edit_queue(programs, lambda record: record.__setitem__("source_value", "unreviewed value"))
    before = authored.read_bytes()
    result = CliRunner().invoke(app, [
        "context", "apply-batch", "--edition", "sample_weekly", "--issue", "1",
        "--programs-root", str(programs), *(["--dry-run"] if dry_run else []),
    ])
    assert result.exit_code == 1
    assert "refused_unbound_approval" in result.output
    assert authored.read_bytes() == before


def test_later_reacceptance_binds_the_reviewed_revision(tmp_path: Path) -> None:
    programs, authored = _workspace(tmp_path)
    _edit_queue(programs, lambda record: (
        record.__setitem__("source_value", "second reviewed value"),
        record.__setitem__("status", "pending"),
    ))
    update_proposal_status(
        "sample", proposal_id="reviewed", new_status="accepted", actor="second-reviewer",
        issue_number=1, programs_root=programs,
    )

    result = _apply(programs)

    assert result.output.startswith("applied:"), result.output
    assert _texts(authored)["assume-1"] == "second reviewed value"


@pytest.mark.parametrize("bound", [True, False])
def test_preexisting_journal_does_not_bypass_payload_binding(tmp_path: Path, bound: bool) -> None:
    programs, authored = _workspace(tmp_path)
    if not bound:
        _edit_queue(programs, lambda record: record.__setitem__("source_value", "unreviewed value"))
    journal = _journal_path("sample", "reviewed", programs_root=programs)
    _write_journal(journal, "reviewed", "yaml_written", note="synthetic interrupted apply")
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    before = authored.read_bytes()
    journal_before = journal.read_bytes()

    result = apply_proposal(proposal, actor="synthetic-principal", programs_root=programs)

    if bound:
        assert result.action == "needs_repair"
        assert "reconcil" in result.note.lower()
    else:
        assert result.action == "refused_unbound_approval"
    assert authored.read_bytes() == before
    assert journal.read_bytes() == journal_before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_interrupted_journal_requires_repair_through_apply_cli(
    tmp_path: Path, batch: bool, dry_run: bool,
) -> None:
    from cli import app

    programs, authored = _workspace(tmp_path)
    journal = _journal_path("sample", "reviewed", programs_root=programs)
    _write_journal(journal, "reviewed", "yaml_written", note="synthetic interrupted apply")
    queue = get_proposals_path("sample", 1, programs_root=programs)
    before = (authored.read_bytes(), journal.read_bytes(), queue.read_bytes())
    arguments = [
        "context", "apply-batch" if batch else "apply",
        "--edition", "sample_weekly", "--issue", "1", "--programs-root", str(programs),
    ]
    if not batch:
        arguments.extend(["--proposal-id", "reviewed"])
    if dry_run:
        arguments.append("--dry-run")

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 3, result.output + repr(result.exception)
    assert "needs_repair" in result.output
    assert "reconcil" in result.output.lower()
    assert (authored.read_bytes(), journal.read_bytes(), queue.read_bytes()) == before
