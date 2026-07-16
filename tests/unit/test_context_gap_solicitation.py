"""ADF-W3.7: unit tests for src/core/context_gap_solicitation.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.context_gap_solicitation import (
    ContextGapSolicitationError,
    approve_solicitation,
    generate_deterministic_solicitation,
    is_in_cooldown,
    record_solicitation_drafted,
    reject_solicitation,
    write_solicitation_draft,
)
from src.core.context_gap_store import RankedGap
from src.core.nudge_models import ResolvedRecipient

_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _gap(*, program: str = "xpf") -> RankedGap:
    return RankedGap(
        feature="workstream_registry",
        program=program,
        lane="deployment",
        field="deep_context",
        severity="quality_degraded",
        impact_estimate="high",
        count=3,
        first_seen=_NOW - timedelta(days=10),
        last_seen=_NOW,
        message="deep_context.why is missing",
        fix_hint="add deep_context.why / .what / .how to workstream_registry.yaml in workstream deployment",
    )


def _recipient() -> ResolvedRecipient:
    return ResolvedRecipient(alias="alex", email="alex@example.com", display_name="Alex")


def _staged_solicitation():
    return generate_deterministic_solicitation(_gap(), recipient=_recipient(), evidence_link="https://vertex/xpf/gaps")


def test_generate_deterministic_solicitation_content() -> None:
    solicitation = _staged_solicitation()
    assert "deep_context.why is missing" in solicitation.current_gap
    assert "deployment" in solicitation.current_gap
    assert "high-impact" in solicitation.why_it_matters
    assert solicitation.requested_action == "add deep_context.why / .what / .how to workstream_registry.yaml in workstream deployment"
    assert solicitation.evidence_link == "https://vertex/xpf/gaps"
    assert solicitation.generation_method == "deterministic"
    assert solicitation.status == "staged"
    assert solicitation.gap_fingerprint == "xpf:workstream_registry:deployment:deep_context"


def test_approve_and_reject_lifecycle() -> None:
    solicitation = _staged_solicitation()
    approved = approve_solicitation(solicitation)
    assert approved.status == "approved"

    rejected = reject_solicitation(_staged_solicitation(), reason="wrong recipient")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "wrong recipient"
    with pytest.raises(ContextGapSolicitationError, match="rejected"):
        approve_solicitation(rejected)


def test_cooldown_false_when_never_drafted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW) is False


def test_cooldown_true_within_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = approve_solicitation(_staged_solicitation())
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW + timedelta(days=5)) is True


def test_cooldown_false_after_window_expires(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = approve_solicitation(_staged_solicitation())
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    assert is_in_cooldown(_gap(), programs_root=programs_root, now=_NOW + timedelta(days=15)) is False


def test_cooldown_is_scoped_to_the_exact_gap_fingerprint(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = approve_solicitation(_staged_solicitation())
    record_solicitation_drafted(solicitation, programs_root=programs_root, now=_NOW)

    different_gap = RankedGap(
        feature="workstream_registry", program="xpf", lane="repair", field="deep_context",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="deep_context.why is missing", fix_hint="add deep_context.why",
    )
    assert is_in_cooldown(different_gap, programs_root=programs_root, now=_NOW) is False


def test_write_draft_requires_approval(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with pytest.raises(ContextGapSolicitationError, match="not 'approved'"):
        write_solicitation_draft(
            _staged_solicitation(), from_email="vertex@example.com", programs_root=programs_root, now=_NOW
        )


def test_write_draft_produces_real_eml_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    solicitation = approve_solicitation(_staged_solicitation())

    draft_path = write_solicitation_draft(
        solicitation, from_email="vertex@example.com", from_display_name="Vertex", programs_root=programs_root, now=_NOW,
    )

    assert draft_path.exists()
    assert draft_path.suffix == ".eml"
    raw = draft_path.read_bytes()
    assert b"X-Unsent: 1" in raw
    assert b"alex@example.com" in raw
    assert b"deep_context.why is missing" in raw


def test_write_draft_lands_in_the_existing_nudge_drafts_dir(tmp_path: Path) -> None:
    from src.core.edition_resolver import get_nudge_paths

    programs_root = tmp_path / "programs"
    solicitation = approve_solicitation(_staged_solicitation())

    draft_path = write_solicitation_draft(
        solicitation, from_email="vertex@example.com", programs_root=programs_root, now=_NOW
    )

    expected_dir = get_nudge_paths("xpf", programs_root=programs_root).drafts_dir
    assert draft_path.parent == expected_dir
