from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exception_queue import (
    ExceptionItem,
    build_exception_queue,
    decorate_exception_queue,
    load_extended_exception_signals,
)
from src.core.jsonl_utils import append_jsonl_line


def test_claim_exception_reads_refuse_repair_and_preserve_corrupt_bytes(
    tmp_path: Path,
) -> None:
    from src.core.claim_tracker import ClaimJournalReadError, load_claim_entries

    root = tmp_path / "programs"
    claims = root / "synthetic" / "journal" / "claims.jsonl"
    claims.parent.mkdir(parents=True)
    claims.write_text('{"broken":\n', encoding="utf-8")

    with pytest.raises(ClaimJournalReadError):
        load_claim_entries("synthetic", programs_root=root, repair_corruption=False)

    assert claims.read_text(encoding="utf-8") == '{"broken":\n'
    assert not (claims.parent / "quarantine").exists()


def test_action_inbox_reads_retained_rotated_action_and_latest_status(tmp_path: Path) -> None:
    from src.core.action_tracker import load_actions
    from src.core.models_v2 import ActionStatus

    root = tmp_path / "programs"
    journal = root / "synthetic" / "journal" / "actions.jsonl"
    old = json.dumps({
        "record_type": "action",
        "id": "old",
        "program_id": "synthetic",
        "text": "old action",
        "owner_alias": "owner",
        "status": "open",
        "source_type": "manual",
        "created_at": "2026-01-01T00:00:00+00:00",
        "due_date": "2026-01-02",
    }) + "\n"
    append_jsonl_line(journal, old, max_bytes=1)
    status = json.dumps({
        "record_type": "status_update",
        "action_id": "old",
        "new_status": "done",
        "updated_at": "2026-11-15T00:00:00+00:00",
        "updated_by": "operator",
        "note": None,
    }) + "\n"
    append_jsonl_line(journal, status, max_bytes=1)

    actions = load_actions(
        "synthetic",
        root,
        repair_corruption=False,
        include_rotated=True,
    )

    assert [action.id for action in actions] == ["old"]
    assert actions[0].status is ActionStatus.DONE


def test_exception_decoration_has_exact_read_only_revalidation_and_authority() -> None:
    item = ExceptionItem(
        kind="overdue_action",
        subject_id="action-1",
        severity_rank=1,
        age_days=4,
        reason="Action overdue.",
        evidence="actions.jsonl#action-1",
    )

    decorated = decorate_exception_queue(
        (item,),
        program_id="synthetic",
        as_of=date(2026, 11, 15),
        extended=False,
    )[0]

    assert decorated.authority_source == "action journal"
    assert decorated.authority_status == "available, retention-limited"
    assert decorated.diff_preview
    assert decorated.expected_effect
    assert decorated.mutation_scope == "none"
    assert decorated.remediation_command == (
        "vertex cockpit exception --program synthetic --kind overdue_action "
        "--subject action-1 --as-of 2026-11-15 --expect-fingerprint "
        f"{decorated.fingerprint}"
    )
    assert "--dry-run" not in decorated.remediation_command


def test_unsafe_exception_identifier_has_no_copyable_remediation() -> None:
    item = ExceptionItem(
        kind="source_gap",
        subject_id="unsafe;echo",
        severity_rank=1,
        age_days=1,
        reason="Missing source.",
        evidence="context-gaps",
    )

    decorated = decorate_exception_queue(
        (item,), program_id="synthetic", as_of=date(2026, 11, 15), extended=True,
    )[0]

    assert decorated.remediation_command is None
    assert decorated.action_guidance.startswith("Manual inspection required")


def test_extended_inbox_claim_reader_is_non_mutating(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    claims = root / "synthetic" / "journal" / "claims.jsonl"
    claims.parent.mkdir(parents=True)
    claims.write_text('{"broken":\n', encoding="utf-8")

    with pytest.raises(ValueError, match="claim journal"):
        load_extended_exception_signals(
            "synthetic", programs_root=root, as_of=date(2026, 11, 15),
        )

    assert claims.read_text(encoding="utf-8") == '{"broken":\n'
    assert not (claims.parent / "quarantine").exists()


def test_exception_html_is_static_keyboard_readable_and_discloses_external_qualification() -> None:
    from src.core.exception_render import (
        render_exception_inbox_html,
        render_exception_protocol_json,
    )

    item = decorate_exception_queue(
        (
            ExceptionItem(
                kind="needs_input",
                subject_id="risk-1",
                severity_rank=0,
                age_days=2,
                reason="Risk level not assessed.",
                evidence="risk_register.yaml#risk-1",
            ),
        ),
        program_id="synthetic",
        as_of=date(2026, 11, 15),
        extended=True,
    )[0]

    html = render_exception_inbox_html(
        "synthetic", date(2026, 11, 15), (item,)
    )

    assert "<main id=\"main\">" in html
    assert "Skip to exception inbox" in html
    assert "Authority status" in html
    assert "Diff / preview" in html
    assert "Exact revalidation" in html
    assert "Mutation scope" in html
    assert "<button" not in html
    assert "<form" not in html
    assert 'src="' not in html

    protocol = render_exception_protocol_json()
    assert protocol["qualification"]["wcag_human_audit"] == "not established"
    assert protocol["qualification"]["operator_proficiency"] == "not established"
    assert protocol["training_protocol"]["qualification_result"] == "fail"


def test_plain_queue_still_sorts_deterministically() -> None:
    queue = build_exception_queue((), (), date(2026, 11, 15))
    assert queue == ()
