"""Auto-discovery of Teams meeting series IDs from display names.

When a meeting series is configured with only a display_name (no series_id),
this module queries the calendar via WorkIQ and returns a resolved series_id if
exactly one high-confidence match is found.  The caller (TeamsDiscoveryProvider)
decides what to do with ambiguous or low-confidence results.
"""
from __future__ import annotations

import logging
from typing import Protocol

from src.core.m365_discovery_support import MatchAliasRule, normalize_match_text
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_calendar_discovery import WorkIQCalendarDiscovery


_LOG = logging.getLogger(__name__)

# Series IDs resolved at confidence >= this threshold are considered unambiguous
# and will be auto-populated in the discovery result without operator review.
_AUTO_RESOLVE_CONFIDENCE_THRESHOLD = 0.90

# Series ID discovery limit per meeting series (kept low — we stop on first exact match)
_RESOLVE_CANDIDATE_LIMIT = 10


class SeriesIdResolver(Protocol):
    """Callable that tries to resolve a series_id for a named meeting series."""

    def __call__(
        self,
        display_name: str,
        *,
        topics: tuple[str, ...],
        match_aliases: tuple[MatchAliasRule, ...],
    ) -> tuple[str, float] | None:
        """Return ``(series_id, confidence)`` if resolved, else ``None``.

        Returns ``None`` when:
        - No WorkIQ/calendar integration is available.
        - No candidates are found.
        - Multiple candidates exceed the confidence threshold (ambiguous).
        - The best candidate's confidence is below ``_AUTO_RESOLVE_CONFIDENCE_THRESHOLD``.
        """
        ...


class CalendarSeriesIdResolver:
    """Concrete resolver backed by WorkIQCalendarDiscovery.

    Instantiated once per gather pass and reused across all meeting series that
    need auto-resolution so the bridge connection is shared.
    """

    def __init__(self, bridge: AgencyBridge) -> None:
        self._discovery = WorkIQCalendarDiscovery.from_bridge(bridge)

    def __call__(
        self,
        display_name: str,
        *,
        topics: tuple[str, ...] = (),
        match_aliases: tuple[MatchAliasRule, ...] = (),
    ) -> tuple[str, float] | None:
        from src.core.m365_discovery_support import use_match_aliases

        try:
            with use_match_aliases(match_aliases):
                candidates = self._discovery.discover_candidates(
                    display_name,
                    limit=_RESOLVE_CANDIDATE_LIMIT,
                    topics=topics,
                )
        except Exception as exc:
            _LOG.warning(
                "series_id auto-resolution failed for %r: %s",
                display_name,
                exc,
            )
            return None

        if not candidates:
            _LOG.debug("series_id auto-resolution: no candidates for %r", display_name)
            return None

        best = candidates[0]

        # Exact-match: accept immediately regardless of match_score numerical value.
        # A second candidate that is also an exact match (same normalized title, different
        # series_id) blocks auto-resolution — operator must choose.
        if best.exact_match:
            if len(candidates) > 1 and candidates[1].exact_match:
                _LOG.warning(
                    "series_id auto-resolution: multiple exact-match candidates for %r "
                    "(%r vs %r) — operator review required",
                    display_name,
                    best.discovered_id,
                    candidates[1].discovered_id,
                )
                return None
            _LOG.info(
                "series_id auto-resolved (exact match) for %r -> %s",
                display_name,
                best.discovered_id,
            )
            return best.discovered_id, 1.0

        # Non-exact: require high match_score and no close second candidate.
        if best.match_score < _AUTO_RESOLVE_CONFIDENCE_THRESHOLD:
            _LOG.debug(
                "series_id auto-resolution: best candidate score %.2f below threshold %.2f for %r",
                best.match_score,
                _AUTO_RESOLVE_CONFIDENCE_THRESHOLD,
                display_name,
            )
            return None

        if len(candidates) > 1 and candidates[1].match_score >= _AUTO_RESOLVE_CONFIDENCE_THRESHOLD:
            _LOG.warning(
                "series_id auto-resolution: ambiguous candidates for %r "
                "(top %.2f vs second %.2f) — operator review required",
                display_name,
                best.match_score,
                candidates[1].match_score,
            )
            return None

        _LOG.info(
            "series_id auto-resolved (score=%.2f) for %r -> %s",
            best.match_score,
            display_name,
            best.discovered_id,
        )
        return best.discovered_id, best.match_score
