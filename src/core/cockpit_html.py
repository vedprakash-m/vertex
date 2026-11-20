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

WCAG 2.2 AA is a *target*; semantic HTML5, landmark roles, a skip link,
visible focus styles, and sufficient color contrast are applied
deliberately. specs/bklg.md BL-H2 action (4), 2026-07-26: a real automated
scan (axe-core 4.4.3, run against actual rendered output, not this file's
source) found and this module fixed one genuine bug -- ``code`` had a fixed
light background with no explicit foreground color, so a client rendering
in dark mode (this file opts into ``color-scheme: light dark``) auto-
inverted the text to white on a near-white chip, an interaction invisible
to any static source scan. Every element that sets a fixed ``background``
now also pins its ``color`` explicitly, rather than leaving it to
scheme-dependent inheritance. An automated scan is not a substitute for a
full independent audit (screen-reader task flows, keyboard-only end-to-end
navigation, and cognitive-load review still need a human) -- that larger
scope is still explicitly out of this module's reach.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from src.core.cockpit_builder import build_cockpit_exception_views
from src.core.cockpit_models import (
    CockpitExceptionView,
    CockpitFinding,
    CockpitSnapshot,
    select_cockpit_exceptions,
)

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


def _display(value: object | None) -> str:
    return str(value) if value not in (None, "") else "not available"


def _render_finding(
    finding: CockpitFinding,
    *,
    retained: bool = False,
    exception_view: CockpitExceptionView | None = None,
) -> str:
    status_label = _STATUS_LABEL.get(finding.status, finding.status)
    owner = escape(finding.owner) if finding.owner else (
        "not available" if exception_view is not None else "unassigned"
    )
    next_command = f"<code>{escape(finding.next_command)}</code>" if finding.next_command else (
        "not available" if exception_view is not None else "none"
    )
    evidence = (
        "".join(f"<li>{_safe_evidence_link(ref)}</li>" for ref in finding.evidence_refs)
        if finding.evidence_refs
        else f"<li>{'not available' if exception_view is not None else 'none'}</li>"
    )
    limitation = (
        "<p>Confidence, current authority, expiry/escalation, diff and expected effect: "
        "not available in this retained finding.</p>" if retained else ""
    )
    next_command_field = (
        ""
        if exception_view is not None
        else f"\n        <dt>Next command</dt><dd>{next_command}</dd>"
    )
    exception_fields = ""
    if exception_view is not None:
        safe_options = "".join(f"<li>{escape(option)}</li>" for option in exception_view.safe_options)
        revalidation = (
            f"<code>{escape(exception_view.revalidation_command)}</code>"
            if exception_view.revalidation_command
            else "not available"
        )
        next_label = (
            "Historical next command (not current guidance)"
            if retained
            else "Next command (after safe revalidation)"
        )
        exception_fields = f"""
        <dt>Exception class</dt><dd>{escape(exception_view.exception_class.value)}</dd>
        <dt>Snapshot</dt><dd>generated {escape(exception_view.snapshot_generated_at.isoformat())};
          input hash <code>{escape(exception_view.snapshot_input_hash)}</code></dd>
        <dt>Confidence</dt><dd>{escape(_display(exception_view.confidence))}</dd>
        <dt>Safe options</dt><dd><ul>{safe_options}</ul></dd>
        <dt>Expected effect</dt><dd>{escape(exception_view.expected_effect)}</dd>
        <dt>Diff / preview</dt><dd>{escape(_display(exception_view.diff_preview))}</dd>
        <dt>{next_label}</dt><dd>{next_command}</dd>
        <dt>Authority required</dt><dd>{escape(_display(exception_view.authority_required))}</dd>
        <dt>Expiry / escalation</dt><dd>{escape(_display(exception_view.expiry_or_escalation))}</dd>
        <dt>Safe revalidation</dt><dd>{revalidation}</dd>"""
    return f"""
    <li class="finding finding-{escape(finding.status)}">
      <span class="finding-status" aria-label="Status: {escape(status_label)}">[{escape(status_label)}]</span>
      <strong>{escape(finding.summary)}</strong>
      <p>{escape(finding.detail)}</p>
      <dl>
        <dt>Finding ID</dt><dd>{escape(finding.finding_id)}</dd>
        <dt>Area</dt><dd>{escape(finding.area)}</dd>
        <dt>Owner</dt><dd>{owner}</dd>{next_command_field}
        <dt>Evidence</dt><dd><ul>{evidence}</ul></dd>
        <dt>Observed</dt><dd>{escape(finding.observed_at.isoformat())}</dd>{exception_fields}
      </dl>{limitation}
    </li>"""


def render_cockpit_html(snapshot: CockpitSnapshot, *, retained: bool = False) -> str:
    """Renders one self-contained HTML document -- no external CSS/JS,
    no network requests, safe to open with ``file://``."""
    program = snapshot.program_summary
    source = snapshot.source_summary
    value = snapshot.value_summary
    risk_color = _RISK_COLOR.get(program.overall_risk, "#57606a")
    exception_views = build_cockpit_exception_views(snapshot, retained=retained)
    exceptions_html = "".join(
        _render_finding(
            finding,
            retained=retained,
            exception_view=view,
        )
        for finding, view in zip(select_cockpit_exceptions(snapshot.findings), exception_views, strict=True)
    )
    exceptions_html = exceptions_html or (
        "<li>No blocked/warn findings in this retained snapshot; this is not current clearance.</li>"
        if retained
        else (
            "<li>No blocked/warn findings were projected at generation time; input completeness "
            "and current clearance are not established.</li>"
        )
    )
    findings_html = "".join(
        _render_finding(
            f, retained=retained
        )
        for f in snapshot.findings
        if f.status not in {"blocked", "warn"}
    ) or "<li>No findings.</li>"
    retained_notice = (
        "<p>Retained cockpit snapshot — Current source and action state not evaluated. "
        "Recorded commands are historical guidance; current CLI revalidation is required.</p>"
        if retained else ""
    )
    edition_label = escape(snapshot.edition_id) if snapshot.edition_id else "(all editions)"
    readiness = f"{program.readiness_percent}%" if program.readiness_percent is not None else "not measured yet"
    blocker_count = str(program.blocker_count) if program.blocker_count is not None else "unknown"
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
  li.finding {{ border-left: 4px solid #57606a; overflow-wrap: anywhere; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem; font-size: 0.9em; }}
  dt {{ font-weight: bold; }}
  code {{ background: #f6f8fa; color: #1f2328; padding: 0.1em 0.3em; border-radius: 0.2em; }}
  .confidence {{ color: #57606a; font-size: 0.85em; }}
  @media (max-width: 40rem) {{ dl {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header>
  <h1>Vertex Cockpit — {escape(snapshot.program_id)} <small>({edition_label})</small></h1>
  <p>Generated {escape(snapshot.generated_at.isoformat())} · as of {escape(snapshot.as_of.isoformat())}</p>{retained_notice}
</header>
<main id="main">
  <section aria-labelledby="program-health">
    <h2 id="program-health">Program health</h2>
    <p><span class="risk-badge">{escape(program.overall_risk.upper())}</span>
       readiness {escape(readiness)} · {blocker_count} blocker(s)</p>
    <p>{"Historical next action (not current guidance)" if retained else "Next action (after safe revalidation)"}:
       {escape(program.next_action) if program.next_action else "none"}</p>
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
  <section aria-labelledby="exceptions">
    <h2 id="exceptions">Exceptions</h2>
    <ul class="findings">{exceptions_html}</ul>
  </section>
  <section aria-labelledby="findings">
    <h2 id="findings">Other findings</h2>
    <ul class="findings">{findings_html}</ul>
  </section>
</main>
<footer>
  <p>Automated evidence does not establish independent human accessibility or operator proficiency.
     Human accessibility and operator proficiency qualification: not established.</p>
</footer>
</body>
</html>
"""


__all__ = ["render_cockpit_html"]
