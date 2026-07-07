"""Tests for the per-source-type auto-resolution gate (discover.md §8.4)."""
from __future__ import annotations

from src.core.discovery_intent import SourceRefKind
from src.core.discovery_resolution import (
    HARD_GATE,
    SOFT_GATE,
    ResolutionContext,
    passes_auto_resolution_gate,
)


def test_gate_blocks_when_not_unique_or_recently_rejected() -> None:
    not_unique = ResolutionContext(confidence=0.99, unique=False, exact_match=True)
    assert not passes_auto_resolution_gate(SourceRefKind.TEAMS_CHAT, not_unique)
    rejected = ResolutionContext(confidence=0.99, unique=True, exact_match=True, recent_rejection=True)
    assert not passes_auto_resolution_gate(SourceRefKind.TEAMS_CHAT, rejected)


def test_gate_passes_on_exact_or_hard_gate_for_any_type() -> None:
    for kind in SourceRefKind:
        assert passes_auto_resolution_gate(kind, ResolutionContext(confidence=0.5, unique=True, exact_match=True))
        assert passes_auto_resolution_gate(kind, ResolutionContext(confidence=HARD_GATE, unique=True))


def test_meeting_series_soft_band_requires_owner_overlap_not_yield() -> None:
    # 0.75–0.85 meeting: needs organizer/attendee overlap; yield alone never unlocks it
    # (an unresolved meeting cannot hydrate transcripts to accrue yield).
    assert not passes_auto_resolution_gate(
        SourceRefKind.MEETING_SERIES, ResolutionContext(confidence=0.80, unique=True)
    )
    assert not passes_auto_resolution_gate(
        SourceRefKind.MEETING_SERIES, ResolutionContext(confidence=0.80, unique=True, nonzero_yield_windows=5)
    )
    assert passes_auto_resolution_gate(
        SourceRefKind.MEETING_SERIES, ResolutionContext(confidence=0.80, unique=True, owner_overlap=True)
    )


def test_teams_chat_soft_band_requires_two_yield_windows() -> None:
    assert not passes_auto_resolution_gate(
        SourceRefKind.TEAMS_CHAT, ResolutionContext(confidence=0.78, unique=True, nonzero_yield_windows=1)
    )
    two = ResolutionContext(confidence=0.78, unique=True, nonzero_yield_windows=2)
    assert passes_auto_resolution_gate(SourceRefKind.TEAMS_CHAT, two)
    assert passes_auto_resolution_gate(SourceRefKind.TEAMS_CHANNEL, two)


def test_email_soft_band_requires_yield_and_continuity() -> None:
    assert not passes_auto_resolution_gate(
        SourceRefKind.EMAIL_THREAD, ResolutionContext(confidence=0.79, unique=True, nonzero_yield_windows=1)
    )
    assert passes_auto_resolution_gate(
        SourceRefKind.EMAIL_THREAD,
        ResolutionContext(confidence=0.79, unique=True, nonzero_yield_windows=1, subject_thread_continuity=True),
    )


def test_below_soft_gate_never_auto_resolves() -> None:
    ctx = ResolutionContext(
        confidence=SOFT_GATE - 0.01, unique=True, owner_overlap=True,
        nonzero_yield_windows=9, subject_thread_continuity=True,
    )
    for kind in SourceRefKind:
        assert not passes_auto_resolution_gate(kind, ctx)
