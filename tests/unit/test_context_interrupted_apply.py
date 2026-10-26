from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from src.core import ncfl_apply
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import load_proposals, stage_extracted_proposals, update_proposal_status


class ProcessInterrupted(BaseException):
    pass


def _workspace(tmp_path: Path, *, bound_current_hash: bool) -> tuple[Path, Path, ContextUpdateProposal]:
    programs = tmp_path / "programs"
    program = programs / "sample"
    program.mkdir(parents=True)
    target = program / "assumptions.yaml"
    target.write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "assumptions": [{
                "id": "sample-assumption", "text": "old text",
                "status": "active", "identified_date": "2024-01-01",
            }],
        }),
        encoding="utf-8",
    )
    pending = ContextUpdateProposal(
        proposal_id="interrupted", program_id="sample", issue_number=1,
        edition_id="sample_weekly", source_type="context_snapshot",
        extracted_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        extractor_version="1.0.0", source_artifact="synthetic.yaml",
        source_field="assumption.text", extraction_method="rule_based",
        target_store="assumptions", target_key="sample-assumption", target_field="text",
        source_value="new text", current_value="old text",
        current_value_hash=hashlib.sha256(b"old text").hexdigest() if bound_current_hash else None,
        confidence="high", batch_eligible=True, extraction_method_rationale="direct extraction",
        conflict_key="assumptions:sample-assumption:text", status="pending",
    )
    stage_extracted_proposals("sample", 1, (pending,), programs_root=programs)
    accepted = update_proposal_status(
        "sample", proposal_id=pending.proposal_id, new_status="accepted",
        actor="reviewer", issue_number=1, programs_root=programs,
    )
    return programs, target, accepted


@pytest.mark.parametrize("boundary,retained_state", [
    ("before_authored", "write_started"),
    ("after_authored", "write_started"),
    ("after_changelog", "yaml_written"),
    ("after_status", "changelog_written"),
    ("after_ledger_journal", "ledger_written"),
])
@pytest.mark.parametrize("bound_current_hash", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_interrupted_apply_refuses_retry_without_changing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    boundary: str, retained_state: str, bound_current_hash: bool, dry_run: bool,
) -> None:
    programs, target, proposal = _workspace(tmp_path, bound_current_hash=bound_current_hash)
    original_target = target.read_bytes()
    journal = ncfl_apply._journal_path("sample", proposal.proposal_id, programs_root=programs)
    changelog = journal.parent / "changelog.jsonl"
    symbol = {
        "before_authored": "_write_journal",
        "after_authored": "_write_to_store",
        "after_changelog": "_write_changelog_entry",
        "after_status": "_update_proposal_status",
        "after_ledger_journal": "_write_journal",
    }[boundary]
    original = getattr(ncfl_apply, symbol)

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if symbol != "_write_journal" or args[2] == retained_state:
            raise ProcessInterrupted(boundary)
        return result

    with monkeypatch.context() as fault:
        fault.setattr(ncfl_apply, symbol, interrupt)
        with pytest.raises(ProcessInterrupted, match=boundary):
            ncfl_apply.apply_proposal(proposal, actor="first", programs_root=programs)

    assert json.loads(journal.read_bytes())["state"] == retained_state
    if boundary == "before_authored":
        assert target.read_bytes() == original_target
    else:
        assert target.read_bytes() != original_target
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["assumptions"][0]["text"] == "new text"
    if boundary in {"after_changelog", "after_status", "after_ledger_journal"}:
        assert len(changelog.read_text(encoding="utf-8").splitlines()) == 1
    else:
        assert not changelog.exists()
    retained = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    assert len(retained.decision_history) == (2 if boundary in {"after_status", "after_ledger_journal"} else 1)
    before = {path: path.read_bytes() for path in programs.rglob("*") if path.is_file()}
    current_read = Mock(wraps=ncfl_apply._read_current_value)
    authored_write = Mock(wraps=ncfl_apply._write_to_store)
    monkeypatch.setattr(ncfl_apply, "_read_current_value", current_read)
    monkeypatch.setattr(ncfl_apply, "_write_to_store", authored_write)

    result = ncfl_apply.apply_proposal(proposal, actor="retry", programs_root=programs, dry_run=dry_run)

    assert result.action == "needs_repair"
    assert "reconcil" in result.note.lower()
    current_read.assert_not_called()
    authored_write.assert_not_called()
    assert {path: path.read_bytes() for path in programs.rglob("*") if path.is_file()} == before
