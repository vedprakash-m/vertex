"""Preview HTML generator for ``vertex setup``.

Generates a sample newsletter HTML preview from a proposed configuration,
using synthetic data derived from real ADO samples (when available) or
fabricated stub data. The preview builds trust and surfaces mismatches
early, before any config files are written.

This module lives in the orchestrator layer (src/commands/) and may import
from src/core/ and src/ai/.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREVIEW_SEED = 42
"""Fixed random seed for deterministic stub data generation."""

WATERMARK_BANNER = "Preview — sample data only"
"""Watermark text inserted at the top of every preview HTML."""

STUB_ITEMS_PER_WORKSTREAM = 4
"""Number of stub work items generated per workstream in the preview."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PreviewWorkstream:
    """Stub workstream section for the preview."""
    name: str
    items: tuple[PreviewItem, ...]


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """Stub work item for the preview."""
    title: str
    risk: str  # "green", "yellow", "red"
    risk_label: str  # text label for accessibility
    owner: str
    eta: str


@dataclass(frozen=True, slots=True)
class PreviewScorecard:
    """Stub scorecard for the preview."""
    name: str
    dimensions: tuple[PreviewDimension, ...]


@dataclass(frozen=True, slots=True)
class PreviewDimension:
    """Stub scorecard dimension for the preview."""
    name: str
    score: str  # e.g. "On Track", "At Risk", "Blocked"
    detail: str


# ---------------------------------------------------------------------------
# Stub data generation
# ---------------------------------------------------------------------------

_RISK_LEVELS = ("green", "yellow", "red")
_RISK_LABELS = {"green": "On Track", "yellow": "At Risk", "red": "Blocked"}
_SCORE_VALUES = ("On Track", "At Risk", "Needs Attention", "Blocked")

_STUB_TITLES = (
    "Implement retry logic for service degradation",
    "Update compliance documentation for Q3 audit",
    "Migrate legacy storage to new platform",
    "Resolve intermittent test failures in CI pipeline",
    "Design capacity planning model for peak load",
    "Review and update incident response runbook",
    "Implement feature flag rollout for canary deployment",
    "Address security findings from latest pen test",
    "Optimize query performance for analytics dashboard",
    "Create onboarding guide for new team members",
    "Set up monitoring alerts for SLA thresholds",
    "Integrate partner API for data synchronization",
)

_STUB_OWNERS = (
    "Alex Chen",
    "Priya Sharma",
    "Jordan Lee",
    "Sam Rivera",
    "Taylor Kim",
)


def generate_preview_data(
    workstream_names: list[str],
    scorecard_names: list[tuple[str, list[str]]],
    *,
    seed: int = PREVIEW_SEED,
) -> tuple[list[PreviewWorkstream], list[PreviewScorecard]]:
    """Generate deterministic stub data for a preview.

    Args:
        workstream_names: List of workstream names.
        scorecard_names: List of (scorecard_name, [dimension_names]) tuples.
        seed: Random seed for reproducibility.

    Returns:
        Tuple of (workstreams, scorecards) with stub data.
    """
    rng = random.Random(seed)

    workstreams: list[PreviewWorkstream] = []
    title_pool = list(_STUB_TITLES)
    rng.shuffle(title_pool)
    title_idx = 0

    for ws_name in workstream_names:
        items: list[PreviewItem] = []
        for _ in range(STUB_ITEMS_PER_WORKSTREAM):
            risk = rng.choice(_RISK_LEVELS)
            title = title_pool[title_idx % len(title_pool)]
            title_idx += 1
            items.append(PreviewItem(
                title=title,
                risk=risk,
                risk_label=_RISK_LABELS[risk],
                owner=rng.choice(_STUB_OWNERS),
                eta=f"2026-{'%02d' % rng.randint(6, 12)}-{'%02d' % rng.randint(1, 28)}",
            ))
        workstreams.append(PreviewWorkstream(name=ws_name, items=tuple(items)))

    scorecards: list[PreviewScorecard] = []
    for sc_name, dim_names in scorecard_names:
        dims: list[PreviewDimension] = []
        for dim_name in dim_names:
            score = rng.choice(_SCORE_VALUES)
            dims.append(PreviewDimension(
                name=dim_name,
                score=score,
                detail=f"Based on {rng.randint(3, 15)} tracked items.",
            ))
        scorecards.append(PreviewScorecard(name=sc_name, dimensions=tuple(dims)))

    return workstreams, scorecards


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_preview_html(
    program_name: str,
    edition_slug: str,
    workstreams: list[PreviewWorkstream],
    scorecards: list[PreviewScorecard],
    *,
    demo: bool = False,
) -> str:
    """Render a standalone preview HTML from stub data.

    The preview includes: watermark banner, health banner, scorecard table,
    workstream sections with stub items, provenance footer, and config summary.

    Accessibility: risk chips include text labels (never color alone),
    table headers use <th scope="col">, body text meets WCAG 2.1 AA contrast.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Health banner: compute risk distribution
    all_items = [item for ws in workstreams for item in ws.items]
    red_count = sum(1 for i in all_items if i.risk == "red")
    yellow_count = sum(1 for i in all_items if i.risk == "yellow")
    green_count = sum(1 for i in all_items if i.risk == "green")
    total = len(all_items) or 1

    bluf = "Mostly on track." if red_count == 0 else f"{red_count} blocked items need attention."

    # Build HTML sections
    risk_color_map = {
        "green": "#2e7d32",
        "yellow": "#f9a825",
        "red": "#c62828",
    }

    # Scorecard HTML
    scorecard_html = ""
    for sc in scorecards:
        rows = ""
        for dim in sc.dimensions:
            rows += f"        <tr><td>{dim.name}</td><td>{dim.score}</td><td>{dim.detail}</td></tr>\n"
        scorecard_html += f"""
    <h3>{sc.name}</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%; max-width:640px;">
      <thead>
        <tr>
          <th scope="col" style="text-align:left;">Dimension</th>
          <th scope="col" style="text-align:left;">Status</th>
          <th scope="col" style="text-align:left;">Detail</th>
        </tr>
      </thead>
      <tbody>
{rows}      </tbody>
    </table>
"""

    # Workstream HTML
    workstream_html = ""
    for ws in workstreams:
        items_html = ""
        for item in ws.items:
            color = risk_color_map.get(item.risk, "#333")
            items_html += (
                f'        <tr>'
                f'<td>{item.title}</td>'
                f'<td><span style="color:{color}; font-weight:bold;">'
                f'{item.risk_label}</span></td>'
                f'<td>{item.owner}</td>'
                f'<td>{item.eta}</td>'
                f'</tr>\n'
            )
        workstream_html += f"""
    <h3>{ws.name}</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%; max-width:640px;">
      <thead>
        <tr>
          <th scope="col" style="text-align:left;">Item</th>
          <th scope="col" style="text-align:left;">Risk</th>
          <th scope="col" style="text-align:left;">Owner</th>
          <th scope="col" style="text-align:left;">ETA</th>
        </tr>
      </thead>
      <tbody>
{items_html}      </tbody>
    </table>
"""

    demo_tag = ' data-demo="true"' if demo else ""

    html = f"""<!DOCTYPE html>
<html lang="en"{demo_tag}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{program_name} — Setup Preview</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; color: #1a1a1a; background: #fafafa; margin: 0; padding: 20px; line-height: 1.5; }}
    .watermark {{ background: #fff3cd; color: #856404; padding: 12px 20px; border: 1px solid #ffc107; border-radius: 4px; margin-bottom: 20px; font-weight: bold; text-align: center; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #fff; padding: 24px; border: 1px solid #e0e0e0; }}
    h1 {{ color: #1a1a1a; font-size: 1.4em; }}
    h2 {{ color: #333; font-size: 1.2em; border-bottom: 1px solid #e0e0e0; padding-bottom: 6px; }}
    h3 {{ color: #444; font-size: 1.05em; }}
    table {{ font-size: 0.9em; }}
    th {{ background: #f5f5f5; }}
    .health-banner {{ background: #e8f5e9; padding: 12px 20px; border-radius: 4px; margin-bottom: 16px; }}
    .health-banner.has-red {{ background: #ffebee; }}
    .footer {{ margin-top: 32px; padding-top: 12px; border-top: 1px solid #e0e0e0; font-size: 0.85em; color: #666; }}
    .config-summary {{ background: #f5f5f5; padding: 12px; border-radius: 4px; margin-top: 16px; font-size: 0.9em; }}
  </style>
</head>
<body>
  <div class="watermark" role="alert">{WATERMARK_BANNER}</div>
  <div class="container">
    <h1>{program_name} Update</h1>
    <p style="color:#666;">Edition: {edition_slug} | Sample Issue #000 | {now}</p>

    <div class="health-banner{' has-red' if red_count > 0 else ''}">
      <strong>Health:</strong> {green_count} on track, {yellow_count} at risk, {red_count} blocked
      <br><strong>BLUF:</strong> {bluf}
      <br><em>Estimated read time: 3 min</em>
    </div>

    <h2>Scorecard</h2>
    {scorecard_html}

    <h2>Workstream Deep Dive</h2>
    {workstream_html}

    <div class="footer">
      <p><strong>Provenance:</strong> Manifest ID: preview-{edition_slug}-000</p>
      <p>Generated by Vertex setup preview. This is sample data — not from live sources.</p>
    </div>

    <div class="config-summary">
      <strong>Config Summary:</strong> This preview was generated from your proposed
      program configuration. {len(workstreams)} workstream(s) and
      {sum(len(sc.dimensions) for sc in scorecards)} scorecard dimension(s) are configured.
    </div>
  </div>
</body>
</html>
"""
    return html


def write_preview(
    html: str,
    output_dir: Path,
) -> Path:
    """Write preview HTML to disk.

    Returns the path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = output_dir / "setup_preview.html"
    preview_path.write_text(html, encoding="utf-8")
    return preview_path
