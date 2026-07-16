"""ADF-W5.5 (specs/arch-data-fix.md Section 10.3): the local static HTML
cockpit renderer.

Section 10.3, verbatim requirements this module implements:
- local static HTML only; no server, no external JavaScript dependency;
- keyboard navigable (semantic landmarks, skip link, no positive tabindex);
- responsive (relative units, no fixed-width layout);
- no raw confidential M365 content (only the same summary/finding fields
  ``render_cockpit_terminal`` already renders -- this module adds no new
  data source);
- all evidence-derived strings are HTML-escaped; URLs/schemes allowlisted;
- no state-changing endpoint (this produces a static file, nothing else);
- system-health status uses labels/icons, not program-risk colors;
- program risk continues to use color.

WCAG 2.2 AA is a *target*, not independently audited by this module (no
automated accessibility-testing tool is wired in) -- semantic HTML5,
landmark roles, a skip link, visible focus styles, and sufficient color
contrast are applied deliberately, but a real assistive-technology pass is
explicitly out of this module's scope (Section 10.3's own note: "must be
reconciled into vertex-ux-spec.md before cockpit enforce mode").

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from src.core.cockpit_models import CockpitFinding, CockpitSnapshot

#: Section 10.3's "URLs and schemes are allowlisted."
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

_RISK_COLOR = {
    "green": "#1a7f37",
    "yellow": "#9a6700",
    "red": "#cf222e",
}

_STATUS_LABEL = {
    "ok": "OK",
    "info": "Info",
    "warn": "Warning",
    "blocked": "Blocked",
}


def _safe_evidence_link(ref: str) -> str:
    """A ref renders as a real anchor only if it parses as an allowlisted
    http(s) URL; every other ref renders as plain escaped text -- never as
    a clickable link to an unvetted scheme (``javascript:``, ``file:``,
    etc.)."""
    parsed = urlparse(ref)
    if parsed.scheme.lower() in _ALLOWED_URL_SCHEMES and parsed.netloc:
        return f'<a href="{escape(ref)}">{escape(ref)}</a>'
    return escape(ref)


def _render_finding(finding: CockpitFinding) -> str:
    status_label = _STATUS_LABEL.get(finding.status, finding.status)
    owner = escape(finding.owner) if finding.owner else "unassigned"
    next_command = f"<code>{escape(finding.next_command)}</code>" if finding.next_command else "none"
    evidence = (
        "".join(f"<li>{_safe_evidence_link(ref)}</li>" for ref in finding.evidence_refs)
        if finding.evidence_refs
        else "<li>none</li>"
    )
    return f"""
    <li class="finding finding-{escape(finding.status)}">
      <span class="finding-status" aria-label="Status: {escape(status_label)}">[{escape(status_label)}]</span>
      <strong>{escape(finding.summary)}</strong>
      <p>{escape(finding.detail)}</p>
      <dl>
        <dt>Area</dt><dd>{escape(finding.area)}</dd>
        <dt>Owner</dt><dd>{owner}</dd>
        <dt>Next command</dt><dd>{next_command}</dd>
        <dt>Evidence</dt><dd><ul>{evidence}</ul></dd>
        <dt>Observed</dt><dd>{escape(finding.observed_at.isoformat())}</dd>
      </dl>
    </li>"""


def render_cockpit_html(snapshot: CockpitSnapshot) -> str:
    """Renders one self-contained HTML document -- no external CSS/JS,
    no network requests, safe to open with ``file://``."""
    program = snapshot.program_summary
    source = snapshot.source_summary
    value = snapshot.value_summary
    risk_color = _RISK_COLOR.get(program.overall_risk, "#57606a")
    findings_html = "".join(_render_finding(f) for f in snapshot.findings) or "<li>No findings.</li>"
    edition_label = escape(snapshot.edition_id) if snapshot.edition_id else "(all editions)"
    readiness = f"{program.readiness_percent}%" if program.readiness_percent is not None else "not measured yet"
    value_metrics = (
        "".join(
            f"<li>{escape(m.label)}: {escape(str(m.value))} {escape(m.unit)} "
            f"<span class=\"confidence\">({escape(m.confidence.value)})</span></li>"
            for m in value.metrics
        )
        or "<li>not measured yet</li>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vertex Cockpit — {escape(snapshot.program_id)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 1rem; line-height: 1.5; max-width: 60rem; margin-inline: auto; }}
  a {{ color: #0969da; }}
  a:focus, button:focus {{ outline: 3px solid #0969da; outline-offset: 2px; }}
  .skip-link {{ position: absolute; left: -999px; top: 0; }}
  .skip-link:focus {{ left: 1rem; top: 1rem; background: #fff; color: #000; padding: 0.5rem; z-index: 10; }}
  header {{ border-bottom: 1px solid #d0d7de; padding-bottom: 1rem; margin-bottom: 1rem; }}
  .risk-badge {{ display: inline-block; padding: 0.2em 0.6em; border-radius: 0.3em; color: #fff; background: {risk_color}; font-weight: bold; }}
  section {{ margin-bottom: 1.5rem; }}
  h2 {{ border-bottom: 1px solid #d0d7de; padding-bottom: 0.3rem; }}
  ul.findings {{ list-style: none; padding: 0; }}
  li.finding {{ border: 1px solid #d0d7de; border-radius: 0.4rem; padding: 0.75rem; margin-bottom: 0.75rem; }}
  li.finding-blocked {{ border-left: 4px solid #cf222e; }}
  li.finding-warn {{ border-left: 4px solid #9a6700; }}
  li.finding-ok {{ border-left: 4px solid #1a7f37; }}
  li.finding-info {{ border-left: 4px solid #57606a; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem; font-size: 0.9em; }}
  dt {{ font-weight: bold; }}
  code {{ background: #f6f8fa; padding: 0.1em 0.3em; border-radius: 0.2em; }}
  .confidence {{ color: #57606a; font-size: 0.85em; }}
  @media (max-width: 40rem) {{ dl {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header>
  <h1>Vertex Cockpit — {escape(snapshot.program_id)} <small>({edition_label})</small></h1>
  <p>Generated {escape(snapshot.generated_at.isoformat())} · as of {escape(snapshot.as_of.isoformat())}</p>
</header>
<main id="main">
  <section aria-labelledby="program-health">
    <h2 id="program-health">Program health</h2>
    <p><span class="risk-badge">{escape(program.overall_risk.upper())}</span>
       readiness {escape(readiness)} · {program.blocker_count} blocker(s)</p>
    <p>Next action: {escape(program.next_action) if program.next_action else "none"}</p>
  </section>
  <section aria-labelledby="source-health">
    <h2 id="source-health">Source health</h2>
    <p>[{"OK" if source.required_healthy == source.required_total else "Degraded"}]
       {source.required_healthy}/{source.required_total} required sources healthy</p>
  </section>
  <section aria-labelledby="value">
    <h2 id="value">Value</h2>
    <ul>{value_metrics}</ul>
  </section>
  <section aria-labelledby="findings">
    <h2 id="findings">Findings</h2>
    <ul class="findings">{findings_html}</ul>
  </section>
</main>
</body>
</html>
"""


__all__ = ["render_cockpit_html"]
