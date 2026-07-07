"""Pure confirm-time signal metrics (FR-SG-27).

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). Both helpers are
pure: they derive a source-health fraction from the gather-state channel summary
and a provenance-confidence fraction from approved signal confidence tiers, with
no I/O and no mutation. ``confirm.py`` imports them under their historical
private aliases.
"""

from __future__ import annotations

from typing import Any


def compute_source_health_pct(gather_state: Any) -> float | None:
    """FR-SG-27: Compute source_health_pct from gather_state channel summary.

    Returns fraction of configured channels that are active with no last_error.
    Returns None if gather_state is unavailable.
    """
    if gather_state is None:
        return None
    channels = getattr(gather_state, "channels", None)
    if not isinstance(channels, dict) or not channels:
        return None
    total = len(channels)
    healthy = sum(
        1
        for ch in channels.values()
        if isinstance(ch, dict)
        and bool(ch.get("active"))
        and not ch.get("last_error")
    )
    return round(healthy / total, 4) if total > 0 else None


def compute_provenance_confidence(approved_signals: tuple) -> float | None:
    """FR-SG-27: Compute provenance_confidence from approved signal confidence tiers.

    Returns fraction of approved signals with source_confidence_tier in {high, medium}.
    Returns None if no signals are available.
    """
    if not approved_signals:
        return None
    high_medium = sum(
        1
        for sig in approved_signals
        if getattr(sig, "source_confidence_tier", "low") in {"high", "medium"}
    )
    return round(high_medium / len(approved_signals), 4)
