"""Chart quality gates extracted from ``src/core/quality_gates``."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.core.quality_gates.models import GateEvaluation, QualityGateReport


def evaluate_chart_gates(
    kusto_sections: tuple[Any, ...],
    *,
    current_time: datetime | None = None,
    edition_charts_enabled: bool = True,
) -> QualityGateReport:
    results: list[GateEvaluation] = []
    now = current_time or datetime.now(timezone.utc)

    if not edition_charts_enabled:
        return QualityGateReport(results=tuple(results))

    chart_sections = [
        section for section in kusto_sections
        if getattr(section, "render_mode", None) in ("chart", "chart_image")
    ]

    qg20_messages: list[str] = []
    for section in chart_sections:
        if not getattr(section, "is_degraded", False):
            continue
        ttl_hours = getattr(section, "chart_cache_ttl_hours", 26)
        cache_at = getattr(section, "cache_captured_at", None) or getattr(section, "captured_at", None)
        if cache_at is None:
            continue
        try:
            age_hours = (now - cache_at).total_seconds() / 3600.0
            threshold = ttl_hours * 1.5
            if age_hours > threshold:
                qg20_messages.append(
                    f"{section.query_id} stale by {age_hours:.0f}h (TTL={ttl_hours}h, threshold={threshold:.0f}h)"
                )
        except Exception:
            pass

    if qg20_messages:
        results.append(
            GateEvaluation(
                gate_id="QG-20",
                passed=False,
                message=f"QG-20: {len(qg20_messages)} chart(s) exceed advisory freshness window: {'; '.join(qg20_messages[:5])}{' ...' if len(qg20_messages) > 5 else ''}",
                exit_code=2,
                forceable=True,
            )
        )
    else:
        results.append(
            GateEvaluation(
                gate_id="QG-20",
                passed=True,
                message="QG-20: All chart sections within advisory freshness window.",
                exit_code=0,
                forceable=True,
            )
        )

    qg21_messages: list[str] = []
    for section in chart_sections:
        size = getattr(section, "chart_png_size_bytes", 0)
        if size > 102_400:
            qg21_messages.append(f"{section.query_id}={size:,}B")

    if qg21_messages:
        results.append(
            GateEvaluation(
                gate_id="QG-21",
                passed=False,
                message=f"QG-21: {len(qg21_messages)} chart PNG(s) exceed hard gate (102,400 B): {'; '.join(qg21_messages[:3])}{' ...' if len(qg21_messages) > 3 else ''}",
                exit_code=3,
                forceable=False,
            )
        )
    else:
        results.append(
            GateEvaluation(
                gate_id="QG-21",
                passed=True,
                message="QG-21: All chart PNGs within size budget.",
                exit_code=0,
                forceable=False,
            )
        )

    qg22_messages: list[str] = []
    for section in chart_sections:
        if not getattr(section, "chart_blocks_publish", False):
            continue
        if not getattr(section, "is_degraded", False):
            continue
        ttl_hours = getattr(section, "chart_cache_ttl_hours", 26)
        cache_at = getattr(section, "cache_captured_at", None) or getattr(section, "captured_at", None)
        if cache_at is None:
            continue
        try:
            age_hours = (now - cache_at).total_seconds() / 3600.0
            if age_hours > ttl_hours:
                qg22_messages.append(f"{section.query_id} stale by {age_hours:.0f}h (TTL={ttl_hours}h)")
        except Exception:
            pass

    if qg22_messages:
        results.append(
            GateEvaluation(
                gate_id="QG-22",
                passed=False,
                message=f"QG-22: {len(qg22_messages)} blocking chart(s) exceed TTL: {'; '.join(qg22_messages[:5])}{' ...' if len(qg22_messages) > 5 else ''}",
                exit_code=3,
                forceable=False,
            )
        )
    else:
        results.append(
            GateEvaluation(
                gate_id="QG-22",
                passed=True,
                message="QG-22: No blocking chart freshness violations.",
                exit_code=0,
                forceable=False,
            )
        )

    return QualityGateReport(results=tuple(results))
