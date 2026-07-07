"""Direct coverage for the extracted confirm pre-validation helpers.

Guards the D-25 / Phase 3 extraction of the authorship + stale-approval cluster
from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/validation.py``. These helpers are read-only:
they resolve the confirming author, validate decision-strip acknowledgements,
and compute stale-approval / stale-decision warnings without writing any state.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from src.commands.confirm_stages.validation import (
    ack_word_count,
    build_stale_proposed_decision_warnings,
    evaluate_stale_approvals,
    is_stale_approval_field,
    read_confirming_author,
    section_has_post_approval_data_change,
    validate_decision_strip_ack,
)
from src.core.overrides_store import DecisionStripAck, OverridesDocument
from src.core.models import ReviewState, WorkItem


def _overrides_document(*, decision_strip_ack: DecisionStripAck | None = None) -> OverridesDocument:
    return OverridesDocument(
        issue_number=1,
        top_3_now=(),
        scorecards=(),
        decision_strip_ack=decision_strip_ack,
    )


def test_is_stale_approval_field() -> None:
    assert is_stale_approval_field("State") is True
    assert is_stale_approval_field("System.State") is True
    assert is_stale_approval_field("Risk Level") is True  # contains "risk"
    assert is_stale_approval_field("Microsoft.VSTS.Common.Risk") is True
    assert is_stale_approval_field("Title") is False
    assert is_stale_approval_field("AssignedTo") is False


def test_read_confirming_author_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_AUTHOR", "  Ada Lovelace  ")
    assert read_confirming_author() == "Ada Lovelace"


def test_read_confirming_author_returns_str_or_none(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_AUTHOR", raising=False)
    result = read_confirming_author()
    assert result is None or isinstance(result, str)


def test_ack_word_count_handles_none_and_split_words() -> None:
    assert ack_word_count(None) == 0
    assert ack_word_count("one two three") == 3


def test_validate_decision_strip_ack_no_ack_or_not_required_passes() -> None:
    assert validate_decision_strip_ack(_overrides_document()) == ()
    assert validate_decision_strip_ack(
        _overrides_document(
            decision_strip_ack=DecisionStripAck(
                no_leadership_ask=False,
                reason="too short",
            ),
        )
    ) == ()


def test_validate_decision_strip_ack_enforces_reason_length_bounds() -> None:
    valid_reason = "one two three four five six seven eight nine ten eleven twelve"
    assert validate_decision_strip_ack(
        _overrides_document(
            decision_strip_ack=DecisionStripAck(
                no_leadership_ask=True,
                reason=valid_reason,
            ),
        )
    ) == ()

    short_reason = "one two three four five six seven eight nine ten eleven"
    assert validate_decision_strip_ack(
        _overrides_document(
            decision_strip_ack=DecisionStripAck(
                no_leadership_ask=True,
                reason=short_reason,
            ),
        )
    ) == ("Decision Strip acknowledgement reason must be 12-40 words.",)

    long_reason = " ".join(f"w{i}" for i in range(41))
    assert validate_decision_strip_ack(
        _overrides_document(
            decision_strip_ack=DecisionStripAck(
                no_leadership_ask=True,
                reason=long_reason,
            ),
        )
    ) == ("Decision Strip acknowledgement reason must be 12-40 words.",)


def _section(section_id: str, state: ReviewState, manifest_id, updated_at):
    return SimpleNamespace(section_id=section_id, state=state, manifest_id=manifest_id, updated_at=updated_at)


def _review(sections):
    return SimpleNamespace(sections=sections)


def _report(items=()):
    return SimpleNamespace(items=items)


def test_evaluate_stale_approvals_no_approved_sections() -> None:
    review = _review([_section("exec_summary", ReviewState.PENDING, None, None)])
    warnings, failures, override = evaluate_stale_approvals(
        review_status=review,
        report=_report(),
        workstream_data=(),
        evidence_by_item={},
        current_manifest_id="m-2",
        ack_stale_approval=False,
    )
    assert warnings == () and failures == () and override is False


def test_evaluate_stale_approvals_current_manifest_not_stale() -> None:
    review = _review([_section("exec_summary", ReviewState.APPROVED, "m-2", datetime(2026, 6, 1))])
    warnings, failures, override = evaluate_stale_approvals(
        review_status=review,
        report=_report(),
        workstream_data=(),
        evidence_by_item={},
        current_manifest_id="m-2",
        ack_stale_approval=False,
    )
    assert warnings == () and failures == () and override is False


def test_evaluate_stale_approvals_stale_without_data_change_warns_only() -> None:
    review = _review([_section("exec_summary", ReviewState.APPROVED, "m-1", None)])
    warnings, failures, override = evaluate_stale_approvals(
        review_status=review,
        report=_report(),
        workstream_data=(),
        evidence_by_item={},
        current_manifest_id="m-2",
        ack_stale_approval=False,
    )
    assert len(warnings) == 1 and "STALE APPROVAL" in warnings[0]
    assert failures == () and override is False


def _evidence_with_change(field: str, changed_date: datetime):
    revision = SimpleNamespace(changed_date=changed_date, fields_changed={field: ("a", "b")})
    return SimpleNamespace(revisions=[revision])


def test_evaluate_stale_approvals_data_changed_blocks_then_acked() -> None:
    approved_at = datetime(2026, 6, 1)
    item = SimpleNamespace(id=42)
    report = _report(items=(item,))
    evidence_by_item = {42: _evidence_with_change("State", datetime(2026, 6, 2))}
    review = _review([_section("exec_summary", ReviewState.APPROVED, "m-1", approved_at)])

    warnings, failures, override = evaluate_stale_approvals(
        review_status=review,
        report=report,
        workstream_data=(),
        evidence_by_item=evidence_by_item,
        current_manifest_id="m-2",
        ack_stale_approval=False,
    )
    assert len(warnings) == 1 and len(failures) == 1 and "BLOCKED" in failures[0]
    assert override is False

    warnings2, failures2, override2 = evaluate_stale_approvals(
        review_status=review,
        report=report,
        workstream_data=(),
        evidence_by_item=evidence_by_item,
        current_manifest_id="m-2",
        ack_stale_approval=True,
    )
    assert failures2 == () and override2 is True


def test_section_has_post_approval_data_change() -> None:
    item = cast(WorkItem, SimpleNamespace(id=1))
    approved_at = datetime(2026, 6, 1)
    # change after approval on a tracked field -> True
    ev = {1: _evidence_with_change("State", datetime(2026, 6, 2))}
    assert section_has_post_approval_data_change((item,), ev, approved_at) is True
    # change before approval -> False
    ev_old = {1: _evidence_with_change("State", datetime(2026, 5, 30))}
    assert section_has_post_approval_data_change((item,), ev_old, approved_at) is False
    # change on untracked field -> False
    ev_untracked = {1: _evidence_with_change("Title", datetime(2026, 6, 2))}
    assert section_has_post_approval_data_change((item,), ev_untracked, approved_at) is False
    # no evidence -> False
    assert section_has_post_approval_data_change((item,), {}, approved_at) is False


def test_build_stale_proposed_decision_warnings_unresolved_edition_returns_empty(tmp_path: Path) -> None:
    reports_root = tmp_path / "repo" / "reports"
    reports_root.mkdir(parents=True)
    # No editions/programs in the repo -> resolve_edition returns None -> ().
    assert build_stale_proposed_decision_warnings(
        edition_name="nonexistent_edition", as_of=date(2026, 6, 5), reports_root=reports_root
    ) == ()
