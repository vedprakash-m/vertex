from __future__ import annotations

from datetime import date
from html import escape

from src.core.exception_queue import DecoratedException


def render_exception_protocol_json() -> dict[str, object]:
    return {
        "schema_version": "1",
        "interaction": "local-read-only",
        "requirements": {
            "evidence": "Every item retains an evidence reference.",
            "diff": "Every item retains a read-only observed diff or preview.",
            "authority": "Every item discloses source and availability status.",
            "action": "Only an exact fingerprint-bound read-only revalidation command is copyable.",
            "mutation": "Inbox and revalidation commands have no mutation scope.",
            "keyboard": (
                "Semantic landmarks, skip link, no controls, and visible focus contract."
            ),
        },
        "qualification": {
            "wcag_automated_contracts": "implemented",
            "wcag_human_audit": "not established",
            "operator_proficiency": "not established",
        },
        "training_protocol": {
            "scenario": (
                "Locate a blocked draft, explain its authority and evidence, compare the "
                "diff, revalidate the exact fingerprint, and stop without mutation."
            ),
            "success_criteria": (
                "Complete with keyboard-only navigation, identify evidence and authority, "
                "detect one intentionally rotated fingerprint, and invoke no write path."
            ),
            "evidence_required": (
                "Named operator, observer, UTC timestamp, completion time, keyboard log, "
                "fingerprint result, and observer approval."
            ),
            "qualification_result": "fail",
        },
    }


def render_exception_inbox_html(
    program_id: str,
    as_of: date,
    items: tuple[DecoratedException, ...],
) -> str:
    rows = "".join(_render_item(item) for item in items) or (
        "<li>No matching exceptions in available inputs. This is not overall clearance.</li>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vertex exception inbox - {escape(program_id)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5; max-width: 72rem; margin: 0 auto; padding: 1rem; }}
  a {{ color: #0969da; }}
  a:focus {{ outline: 3px solid #0969da; outline-offset: 2px; }}
  .skip {{ position: absolute; left: -999px; }}
  .skip:focus {{ left: 1rem; top: 1rem; background: #fff; color: #000;
    padding: .5rem; z-index: 10; }}
  ul {{ list-style: none; padding: 0; }}
  li.item {{ border: 1px solid #8c959f; border-left: .25rem solid #57606a;
    border-radius: .25rem; margin: 0 0 1rem; padding: .75rem;
    overflow-wrap: anywhere; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .25rem 1rem; }}
  dt {{ font-weight: 700; }}
  code {{ background: #f6f8fa; color: #1f2328; overflow-wrap: anywhere; }}
  .qualification {{ border: 1px solid #8c959f; padding: .75rem; }}
  @media (max-width: 40rem) {{ dl {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to exception inbox</a>
<header>
  <h1>Vertex exception inbox - {escape(program_id)}</h1>
  <p>Current files evaluated as of {escape(as_of.isoformat())}; this is not historical replay.</p>
</header>
<main id="main">
  <section aria-labelledby="items-heading">
    <h2 id="items-heading">Exceptions ({len(items)})</h2>
    <ul>{rows}</ul>
  </section>
  <aside class="qualification" aria-labelledby="qualification-heading">
    <h2 id="qualification-heading">Qualification</h2>
    <p>WCAG 2.2 AA human audit: not established.</p>
    <p>Operator proficiency evidence: not established.</p>
    <p>The inbox is a local read-only surface and contains no action controls.</p>
  </aside>
</main>
</body>
</html>
"""


def _render_item(item: DecoratedException) -> str:
    command = (
        f"<code>{escape(item.remediation_command)}</code>"
        if item.remediation_command
        else "not available"
    )
    return f"""
<li class="item">
  <h3>{escape(item.kind)} - {escape(item.subject_id)}</h3>
  <p>{escape(item.reason)}</p>
  <dl>
    <dt>Age</dt><dd>{item.age_days} day(s)</dd>
    <dt>Evidence</dt><dd>{escape(item.evidence)}</dd>
    <dt>Authority source</dt><dd>{escape(item.authority_source)}</dd>
    <dt>Authority status</dt><dd>{escape(item.authority_status)}</dd>
    <dt>Diff / preview</dt><dd>{escape(item.diff_preview)}</dd>
    <dt>Expected effect</dt><dd>{escape(item.expected_effect)}</dd>
    <dt>Mutation scope</dt><dd>{escape(item.mutation_scope)}</dd>
    <dt>Exact revalidation</dt><dd>{command}</dd>
    <dt>Fingerprint</dt><dd><code>{escape(item.fingerprint)}</code></dd>
    <dt>Action guidance</dt><dd>{escape(item.action_guidance)}</dd>
  </dl>
</li>"""


__all__ = ["render_exception_inbox_html", "render_exception_protocol_json"]
