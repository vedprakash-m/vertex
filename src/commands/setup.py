"""Conversational auto-discovery concierge for ``vertex setup``.

This module implements the ``vertex setup`` command — a conversational,
AI-assisted onboarding surface that replaces the manual ``vertex onboard``
wizard for first-time users. It builds on top of ``OnboardDraft`` and
``_finalize_onboarding`` from ``onboard.py`` for the final file write,
but adds a state machine, confidence tracking, session persistence, and
a preview-driven workflow.
"""
from __future__ import annotations

import os
import re
import textwrap
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Annotated, Callable

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import process_generated_text
from src.commands.onboard import ADOStage, IdentityStage
from src.core.setup_state import (
    ConversationStateMachine,
    SetupDraft,
    generate_edition_slug,
    load_session,
    save_session,
)

# ---------------------------------------------------------------------------
# Banner / chrome
# ---------------------------------------------------------------------------

_BANNER = (
    "  _   __          __\n"
    " | | / /__ _____/ /____ ___ __\n"
    " | |/ / -_) __/ __/ -_) \\ /\n"
    " |___/\\__/_/  \\__/\\__/_\\_\\\\\n"
    "\n"
    " Vertex -- governed TPM intelligence platform\n"
)

_STEP_LABELS = {
    "identity":    "Step 1/4  Identity",
    "workstreams": "Step 2/4  Workstreams",
    "ado":         "Step 3/4  ADO configuration",
    "review":      "Step 4/4  Review & confirm",
}


def _banner() -> None:
    typer.echo(_BANNER)


def _step(name: str) -> None:
    label = _STEP_LABELS.get(name, name)
    line = "-" * 60
    typer.echo(f"\n{line}")
    typer.echo(f"  {label}")
    typer.echo(line)


def _info(msg: str) -> None:
    typer.echo(f"  {msg}")


def _ok(msg: str) -> None:
    typer.echo(f"  [ok] {msg}")


def _warn(msg: str) -> None:
    typer.echo(f"  [!]  {msg}")


# ---------------------------------------------------------------------------
# _ask() — unified prompt with ? help, defaults, and optional validation
# ---------------------------------------------------------------------------

def _ask(
    prompt: str,
    *,
    help_text: str = "",
    default: str = "",
    validator: Callable[[str], str | None] | None = None,
    required: bool = False,
) -> str:
    """Prompt the user, supporting ? for help and Enter for defaults.

    Returns the confirmed value (stripped). If required=True and the user
    keeps providing empty input, loops until a non-empty value is entered.
    """
    hint = f" [{default}]" if default else ""
    full_prompt = f"\n  {prompt}{hint}\n  > "

    while True:
        raw = input(full_prompt).strip()

        if raw == "?":
            if help_text:
                for line in textwrap.wrap(help_text, width=60):
                    typer.echo(f"      {line}")
            else:
                typer.echo("      No help available for this field.")
            continue

        value = raw if raw else default

        if required and not value:
            _warn("This field is required. Type ? for help.")
            continue

        if validator and value:
            error = validator(value)
            if error:
                _warn(error)
                continue

        return value


def _ask_yn(prompt: str, *, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    raw = input(f"\n  {prompt} {hint}\n  > ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


def _validate_email(v: str) -> str | None:
    if not _EMAIL_RE.match(v):
        return "Expected format: name@domain.com"
    return None


def _validate_slug(v: str) -> str | None:
    if not _SLUG_RE.match(v):
        return "Use lowercase letters, digits, and underscores only."
    return None


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

_DEMO_PROGRAM = "Acme Platform Reliability"
_DEMO_ID = "acme_platform"
_DEMO_OBJECTIVE = "Ship Acme Platform GA with zero P0 regressions by Q3."
_DEMO_AUTHOR = "Alex Chen"
_DEMO_EMAIL = "alex@example.com"
_DEMO_WORKSTREAMS = [
    ("Infra Health", "Track infra stability and SLA compliance"),
    ("Feature Delivery", "Monitor GA features against ship criteria"),
    ("Risk & Compliance", "Surface open risks and compliance blockers"),
]
_DEMO_ADO_ORG = "your-org"
_DEMO_ADO_PROJECT = "your-project"


# ---------------------------------------------------------------------------
# AI workstream suggester (best-effort; degrades gracefully)
# ---------------------------------------------------------------------------

def _ai_suggest_workstreams(description: str) -> list[tuple[str, str]]:
    """Try to use the AI provider to propose workstreams from a description.

    Returns list of (name, description) tuples, or empty list on failure.
    """
    if get_ai_mode() == AIMode.DISABLED:
        return []
    try:
        from src.ai.deployment_fallback import FallbackStructuredClient, resolve_ai_deployments_for_feature

        deployments = resolve_ai_deployments_for_feature(
            feature_name="onboard_assistant",
            primary_candidates=(),
            backup_candidates=(),
            primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            return []

        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=0.3,
            budget_usd=0.2,
        )
        system = (
            "You are a TPM onboarding assistant. "
            "Given a program description, return a JSON object with key 'workstreams' "
            "containing a list of objects, each with 'name' (2-4 words) and 'description' "
            "(one sentence). Return 3-5 workstreams. Return JSON only."
        )
        user = f"Program description: {description}"
        result: list[tuple[str, str]] = client.structured(
            system,
            user,
            parser=_parse_ws_suggestions,
            max_tokens=300,
            prompt_version="setup_ws_suggest.v1",
        )
        return result

    except Exception:  # noqa: BLE001 — graceful degradation
        return []


def _parse_ws_suggestions(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("workstreams", [])
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        desc = item.get("description", "")
        if isinstance(name, str) and name.strip():
            safe_name = process_generated_text(name.strip()).text
            safe_desc = process_generated_text(desc.strip() if isinstance(desc, str) else "").text
            if safe_name:
                result.append((safe_name, safe_desc))
    return result


# ---------------------------------------------------------------------------
# Live ADO discovery helper (best-effort; degrades gracefully)
# ---------------------------------------------------------------------------


def _try_live_ado_area_discovery(org: str, project: str) -> tuple[str, ...]:
    """Attempt live ADO area path discovery for the given org/project.

    Returns a sorted tuple of distinct area paths from work items changed in
    the last 90 days, or an empty tuple if ADO is unreachable or auth fails.
    """
    try:
        from src.core.ado_client import ADOClient
        client = ADOClient(organization=org, project=project, show_progress=False)
        return client.list_area_paths(days=90, top=100)
    except Exception:
        return ()


# ---------------------------------------------------------------------------
# Collect phases
# ---------------------------------------------------------------------------

def _collect_identity(draft: SetupDraft) -> None:
    _step("identity")
    _info("Let's start with the basics about your program.\n")

    program_name = _ask(
        "Program name",
        help_text=(
            "A short, human-readable name for your program. "
            "Example: 'Acme Platform Reliability' or 'Storage Compliance'. "
            "This will appear in the newsletter subject line."
        ),
        required=True,
    )

    slug = generate_edition_slug(program_name)
    program_id = slug.replace("_weekly", "")
    _ok(f"Edition slug: {slug}  |  Program ID: {program_id}")

    objective = _ask(
        "One-sentence objective",
        help_text=(
            "The single most important outcome your program is trying to achieve. "
            "Example: 'Ship GA by Q3 with zero P0 regressions.' "
            "This anchors the AI when generating your newsletter."
        ),
        default=f"Track {program_name} milestones and risks.",
    )

    author_name = _ask(
        "Your display name",
        help_text="How your name appears in the newsletter footer. Example: Alex Chen",
        required=True,
    )

    author_email = _ask(
        "Your email address",
        help_text=(
            "Used as the sender address when publishing the newsletter. "
            "Example: alex@example.com"
        ),
        validator=_validate_email,
        required=True,
    )

    cadence = _ask(
        "Cadence",
        help_text="How often to publish. Options: weekly, biweekly, monthly.",
        default="weekly",
    )
    send_day = _ask(
        "Send day",
        help_text="Day of week for the newsletter. Example: monday",
        default="monday",
    )

    draft.identity = IdentityStage(
        program_name=program_name,
        program_id=program_id,
        objective=objective,
        mission=objective,
        newsletter_title=f"{program_name} Update",
        cadence=cadence if cadence in ("daily", "weekly", "biweekly", "monthly") else "weekly",
        author_display_name=author_name,
        author_email=author_email,
        send_day=send_day if send_day in (
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
        ) else "monday",
        send_time_local="09:00",
        timezone="America/Los_Angeles",
        current_phase="active",
        key_dependency_chain=(),
    )
    draft.field_confidence["identity.program_name"] = "user_confirmed"
    draft.field_confidence["identity.author_email"] = "user_confirmed"


def _collect_workstreams(
    draft: SetupDraft,
    *,
    from_description: str | None = None,
    auto: bool = False,
) -> list[tuple[str, str]]:
    """Collect workstreams. Returns list of (name, description)."""
    _step("workstreams")
    _info(
        "Workstreams are flexible groupings of related work — like tags across ADO items.\n"
        "  They don't have to match your org chart. Type ? at any prompt for help.\n"
    )

    ai_suggestions: list[tuple[str, str]] = []
    description = from_description or (
        draft.identity.objective if isinstance(draft.identity, IdentityStage) else ""
    )

    if description:
        _info("Asking AI to suggest workstreams from your program description...")
        ai_suggestions = _ai_suggest_workstreams(description)

    workstreams: list[tuple[str, str]] = []

    if ai_suggestions:
        _info(f"AI suggested {len(ai_suggestions)} workstreams:\n")
        for idx, (name, desc) in enumerate(ai_suggestions, 1):
            _info(f"  {idx}. {name}")
            if desc:
                for line in textwrap.wrap(desc, width=52):
                    _info(f"       {line}")

        if auto or _ask_yn("Use these suggestions as a starting point?", default=True):
            workstreams = list(ai_suggestions)
            _ok(f"Accepted {len(workstreams)} AI-suggested workstreams.")
            if not auto:
                if _ask_yn("Would you like to add or remove any?", default=False):
                    workstreams = _edit_workstreams(workstreams)
            draft.field_confidence["structure.workstreams"] = "user_confirmed"
            return workstreams

    # Manual collection
    workstreams = _edit_workstreams([])
    draft.field_confidence["structure.workstreams"] = "user_confirmed"
    return workstreams


def _edit_workstreams(existing: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Interactively add/edit workstreams."""
    workstreams = list(existing)

    if not workstreams:
        _info("Enter your workstreams one at a time. Press Enter on an empty line when done.\n")

    ws_idx = len(workstreams) + 1
    while True:
        prompt = f"Workstream {ws_idx} name (or Enter to finish)"
        name = _ask(
            prompt,
            help_text=(
                "A workstream is a named area of work in your program. "
                "Examples: 'Infra Health', 'Feature Delivery', 'Risk & Compliance'. "
                "Use 2-4 words."
            ),
        )
        if not name:
            break

        desc = _ask(
            f"  One-sentence description for '{name}'",
            help_text="Optional. Describe what this workstream tracks.",
            default="",
        )

        workstreams.append((name, desc))
        ws_idx += 1

    if not workstreams:
        _warn("At least one workstream is required.")
        return _edit_workstreams([])

    return workstreams


def _collect_ado(
    draft: SetupDraft,
    workstreams: list[tuple[str, str]],
    *,
    manual: bool = False,
) -> dict[str, tuple[str, ...]]:
    _step("ado")
    _info(
        "ADO integration lets Vertex pull live work item data into your newsletter.\n"
        "  You can configure this now or fill in placeholders and update later.\n"
    )

    program_id = draft.identity.program_id if isinstance(draft.identity, IdentityStage) else "your-program"

    org = _ask(
        "ADO organization",
        help_text=(
            "Your Azure DevOps organization name. "
            "Found in the URL: dev.azure.com/{org}. "
            "Example: contoso"
        ),
        default="your-org",
    )

    project = _ask(
        "ADO project",
        help_text=(
            "Your ADO project name. "
            "Found in the URL: dev.azure.com/{org}/{project}. "
            "Example: Platform"
        ),
        default="your-project",
    )

    # --- Live ADO discovery (Phase B) ---
    discovered_areas: tuple[str, ...] = ()
    if not manual and org != "your-org" and project != "your-project":
        _info("  Probing ADO for area paths…")
        discovered_areas = _try_live_ado_area_discovery(org, project)
        if discovered_areas:
            _ok(f"Found {len(discovered_areas)} area path(s). Showing relevant suggestions at each prompt.")
            draft.ado_discovery_used = True
        else:
            _info("  ADO discovery unavailable — enter area paths manually.")

    _info(
        "\n  For each workstream, enter its ADO area path (or press Enter for a placeholder).\n"
        "  Example: One\\Storage\\Compliance"
    )

    area_paths: list[str] = []
    ws_area_paths: dict[str, tuple[str, ...]] = {}

    for name, _desc in workstreams:
        # When live discovery succeeded, show the most relevant area paths as context.
        if discovered_areas:
            name_lower = name.lower()
            relevant = [
                ap for ap in discovered_areas
                if any(part.lower() in ap.lower() for part in name_lower.split())
            ][:5] or list(discovered_areas[:5])
            _info(f"\n  Suggested area paths for '{name}':")
            for ap in relevant:
                _info(f"    {ap}")

        ap = _ask(
            f"  Area path for '{name}'",
            help_text=(
                "ADO area path that groups this workstream's work items. "
                "Use backslash as separator. Example: Platform\\Reliability\\Infra"
            ),
            default=(
                relevant[0] if discovered_areas and relevant else
                f"One\\{program_id}\\{name.replace(' ', '_')}"
            ),
        )
        ws_area_paths[name] = (ap,)
        if ap not in area_paths:
            area_paths.append(ap)

    draft.ado = ADOStage(
        organization=org,
        project=project,
        area_paths=tuple(area_paths),
        work_item_types=("Feature", "Risk", "Scenario", "Key Result"),
        excluded_states=("Removed", "Cut"),
        date_window_days=30,
        api_timeout_seconds=30,
    )
    draft.field_confidence["ado.organization"] = "user_confirmed" if org != "your-org" else "default"
    draft.field_confidence["ado.project"] = "user_confirmed" if project != "your-project" else "default"

    return ws_area_paths


def _build_structure(
    draft: SetupDraft,
    workstreams: list[tuple[str, str]],
    ws_area_paths: dict[str, tuple[str, ...]],
) -> None:
    """Build the StructureStage, ScorecardStage, PeopleStage, StyleStage."""
    from src.commands.onboard import (
        DimensionStage,
        PeopleStage,
        ScorecardStage,
        StructureStage,
        StyleStage,
        WorkstreamStage,
    )

    identity = draft.identity
    program_id = identity.program_id if isinstance(identity, IdentityStage) else "program"
    author_email = identity.author_email if isinstance(identity, IdentityStage) else "author@example.com"

    ws_stages: list[WorkstreamStage] = []
    scorecard_stages: list[ScorecardStage] = []

    for name, desc in workstreams:
        ws_id = name.lower().replace(" ", "_").replace("&", "and")
        area_paths = ws_area_paths.get(name, (f"One\\{program_id}\\{ws_id}",))

        ws_stages.append(WorkstreamStage(
            name=name,
            aliases=(ws_id,),
            area_paths=area_paths,
            dri_email=author_email,
            alternate_owner=None,
            description=desc or f"Workstream for {name}",
            why_it_matters=None,
            history_summary=None,
            leadership_sensitivity=None,
            current_blocker=None,
        ))

        dim_name = f"{name} Health"
        ado_filter = f"area_path contains '{area_paths[0]}'" if area_paths else ""
        scorecard_stages.append(ScorecardStage(
            name=name,
            dimensions=(
                DimensionStage(
                    name=dim_name,
                    description=f"Overall health of {name}",
                    ado_filter=ado_filter,
                    workstream_id=ws_id,
                ),
            ),
        ))

    draft.structure = StructureStage(
        edition_type="detailed",
        scorecards=tuple(scorecard_stages),
    )
    draft.people = PeopleStage(
        workstreams=tuple(ws_stages),
        reviewers=(),
        leadership_readers=(),
        workstream_owners=(),
    )
    draft.style = StyleStage(
        glossary=(),
        extra_banned_phrases=(),
        voice=None,
        structure=None,
    )


def _show_review(draft: SetupDraft, workstreams: list[tuple[str, str]]) -> bool:
    """Print a summary and ask for confirmation. Returns True to proceed."""
    _step("review")
    identity = draft.identity
    ado = draft.ado

    program_name = identity.program_name if isinstance(identity, IdentityStage) else "Unknown"
    program_id = identity.program_id if isinstance(identity, IdentityStage) else "unknown"
    edition_slug = generate_edition_slug(program_name)

    typer.echo("\n  Configuration summary:")
    typer.echo("  " + "." * 56)
    typer.echo(f"  Program       : {program_name}")
    typer.echo(f"  Edition slug  : {edition_slug}")
    typer.echo(f"  Objective     : {identity.objective if isinstance(identity, IdentityStage) else '-'}")
    typer.echo(f"  Author        : {identity.author_display_name if isinstance(identity, IdentityStage) else '-'} "
               f"<{identity.author_email if isinstance(identity, IdentityStage) else '-'}>")
    typer.echo(f"  Cadence       : {identity.cadence if isinstance(identity, IdentityStage) else '-'} "
               f"({identity.send_day if isinstance(identity, IdentityStage) else '-'})")

    if isinstance(ado, ADOStage):
        typer.echo(f"  ADO org       : {ado.organization}")
        typer.echo(f"  ADO project   : {ado.project}")

    typer.echo(f"\n  Workstreams ({len(workstreams)}):")
    for name, desc in workstreams:
        typer.echo(f"    - {name}" + (f": {desc}" if desc else ""))

    typer.echo("\n  Files to be created:")
    typer.echo(f"    programs/{program_id}/program.yaml")
    typer.echo(f"    editions/{edition_slug}.yaml")
    typer.echo(f"    programs/{program_id}/readiness.yaml")
    typer.echo(f"    programs/{program_id}/trusted_baseline.yaml")
    typer.echo("    ... and supporting KB/risk/decision stubs")
    typer.echo("  " + "." * 56)

    return _ask_yn("\nLooks good — write these files?", default=True)


def _show_next_steps(edition_slug: str, program_id: str) -> None:
    typer.echo("\n" + "=" * 60)
    typer.echo("  Setup complete. Next steps:")
    typer.echo("=" * 60)
    typer.echo("")
    typer.echo(f"  1. Validate your configuration:")
    typer.echo(f"       vertex doctor --edition {edition_slug}")
    typer.echo("")
    typer.echo(f"  2. Preview your first newsletter (no live data needed):")
    typer.echo(f"       vertex report --edition {edition_slug} --dry-run")
    typer.echo("")
    typer.echo(f"  3. Pull live ADO data and generate a draft:")
    typer.echo(f"       vertex gather --program {program_id}")
    typer.echo(f"       vertex report --edition {edition_slug}")
    typer.echo("")
    typer.echo("  Tip: run `vertex doctor` after any config change to catch")
    typer.echo("  schema errors early.\n")


def _run_preview(
    draft: SetupDraft,
    workstreams: list[tuple[str, str]],
    *,
    output_dir: Path,
    no_open: bool = False,
) -> None:
    """Generate an HTML preview from the proposed config and optionally open it."""
    from src.commands.setup_preview import generate_preview_data, render_preview_html, write_preview

    identity = draft.identity
    program_name = identity.program_name if isinstance(identity, IdentityStage) else "Preview Program"
    edition_slug = generate_edition_slug(program_name)
    program_id = identity.program_id if isinstance(identity, IdentityStage) else "program"

    ws_names = [name for name, _ in workstreams]
    scorecard_names = [(name, [f"{name} Health"]) for name, _ in workstreams]

    ws_data, sc_data = generate_preview_data(ws_names, scorecard_names)
    html = render_preview_html(program_name, edition_slug, ws_data, sc_data)
    preview_path = write_preview(html, output_dir)

    typer.echo(f"\n  Preview written to: {preview_path}")

    if not no_open:
        try:
            import webbrowser
            webbrowser.open(preview_path.as_uri())
            _ok("Opened preview in browser.")
        except Exception:  # noqa: BLE001
            _info("Could not open browser. Open the file manually.")

    _show_next_steps(edition_slug, program_id)


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------

def _run_demo() -> None:
    """Show a realistic demo of what setup produces. No files are written."""
    _banner()
    typer.echo("  Demo mode: showing what setup produces for a fictional program.\n")
    typer.echo("  (No files will be written in demo mode.)\n")

    _step("identity")
    _info(f"Program name    : {_DEMO_PROGRAM}")
    _info(f"Edition slug    : {_DEMO_ID}_weekly")
    _info(f"Objective       : {_DEMO_OBJECTIVE}")
    _info(f"Author          : {_DEMO_AUTHOR} <{_DEMO_EMAIL}>")
    _info(f"Cadence         : weekly (monday)")

    _step("workstreams")
    _info(f"Workstreams ({len(_DEMO_WORKSTREAMS)}):")
    for name, desc in _DEMO_WORKSTREAMS:
        _info(f"  - {name}: {desc}")

    _step("ado")
    _info(f"ADO org     : {_DEMO_ADO_ORG}")
    _info(f"ADO project : {_DEMO_ADO_PROJECT}")
    for name, desc in _DEMO_WORKSTREAMS:
        ws_id = name.lower().replace(" ", "_").replace("&", "and")
        _info(f"  {name}: One\\{_DEMO_ID}\\{ws_id}")

    _step("review")
    typer.echo("\n  Files that would be created:")
    typer.echo(f"    programs/{_DEMO_ID}/program.yaml")
    typer.echo(f"    editions/{_DEMO_ID}_weekly.yaml")
    typer.echo(f"    programs/{_DEMO_ID}/readiness.yaml")
    typer.echo(f"    programs/{_DEMO_ID}/trusted_baseline.yaml")
    typer.echo(f"    programs/{_DEMO_ID}/kb/overview.yaml")
    typer.echo(f"    programs/{_DEMO_ID}/risk_register.yaml")

    typer.echo("\n" + "=" * 60)
    typer.echo("  End of demo. Run without --demo to set up your program.")
    typer.echo("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

def setup_command(
    from_description: Annotated[
        str | None,
        typer.Option(
            "--from-description",
            help="One-line program description. Skips greeting; enters auto-discovery.",
        ),
    ] = None,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Show what setup produces for a fictional program. No files written."),
    ] = False,
    preview: Annotated[
        bool,
        typer.Option("--preview", help="Show config YAML to stdout without writing files."),
    ] = False,
    auto: Annotated[
        bool,
        typer.Option("--auto", help="Accept AI suggestions automatically; minimize interactive prompts."),
    ] = False,
    auto_confirm: Annotated[
        bool,
        typer.Option(
            "--auto-confirm",
            help="Skip the review step and write files immediately. Only valid with --from-description.",
        ),
    ] = False,
    advanced: Annotated[
        bool,
        typer.Option("--advanced", help="Expose all fields, including optional ones."),
    ] = False,
    manual: Annotated[
        bool,
        typer.Option(
            "--manual",
            help="Skip ADO discovery entirely; collect all values interactively.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Resume from a saved .vertex/setup_session_*.json file.",
        ),
    ] = False,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Suppress automatic browser opening after preview."),
    ] = False,
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Update an existing program/edition config.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Output generated YAML to stdout without writing files.",
        ),
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Where to write preview and session files."),
    ] = Path("."),
    ado_org: Annotated[
        str | None,
        typer.Option("--ado-org", help="ADO organization (skips ADO org prompt)."),
    ] = None,
    ado_project: Annotated[
        str | None,
        typer.Option("--ado-project", help="ADO project (skips ADO project prompt)."),
    ] = None,
) -> None:
    """Conversational, AI-assisted setup for a new Vertex program.

    Creates a working program configuration through a short guided conversation.
    Type ? at any prompt for contextual help.

    Quick start:
      vertex setup
      vertex setup --demo
      vertex setup --from-description "Platform reliability weekly for the Acme team"
    """
    if auto_confirm and not from_description:
        raise typer.BadParameter("--auto-confirm requires --from-description.")
    if dry_run:
        no_open = True

    # --- Demo mode: no files, just show what it looks like
    if demo:
        _run_demo()
        raise typer.Exit(code=0)

    # --- Resume mode
    workspace = Path(output_dir).resolve()
    if resume:
        result = load_session(workspace)
        if result is None:
            typer.echo("No saved session found.")
            raise typer.Exit(code=1)
        draft, sm, _ = result
        typer.echo(f"Resuming session from state: {sm.current}")
    else:
        draft = SetupDraft()
        sm = ConversationStateMachine(draft)

        _banner()
        typer.echo(
            "  Welcome. I'll help you set up a new Vertex program newsletter.\n"
            "  This takes about 5 minutes. Type ? at any prompt for contextual help.\n"
            "  Press Ctrl-C at any time to cancel (no files will be written).\n"
        )

        # If a description was provided, pre-fill the program name
        if from_description:
            _info(f"Setting up from description: {from_description}")

    try:
        # Step 1 — Identity
        if draft.identity is None:
            _collect_identity(draft)
        sm.transition("identity")

        # Step 2 — Workstreams
        workstreams = _collect_workstreams(
            draft,
            from_description=from_description,
            auto=auto,
        )
        sm.transition("ado_probe")

        # Step 3 — ADO (with optional live discovery)
        ws_area_paths = _collect_ado(draft, workstreams, manual=manual)
        # Transition through ado_discovery state when live ADO was used,
        # otherwise take the direct ado_probe → structure_propose path.
        if draft.ado_discovery_used:
            sm.transition("ado_discovery")
        sm.transition("structure_propose")

        # Build internal structure
        _build_structure(draft, workstreams, ws_area_paths)

        # Step 4 — Review & confirm
        sm.transition("review")
        if not auto_confirm and not dry_run and not preview:
            confirmed = _show_review(draft, workstreams)
            if not confirmed:
                _warn("Setup cancelled. Run `vertex setup` to start over.")
                save_session(draft, sm, workspace)
                _info("Session saved. Run `vertex setup --resume` to continue.")
                raise typer.Exit(code=1)
        elif preview or dry_run:
            _show_review(draft, workstreams)

        sm.transition("write")

        # Step 5 — Write files
        from src.commands.onboard import OnboardDraft as OnboardDraft
        from src.commands.onboard import (
            OnboardPaths,
            _build_documents,
            _finalize_onboarding,
            _resolve_onboard_paths,
        )

        identity = draft.identity
        if not isinstance(identity, IdentityStage):
            raise typer.BadParameter("Identity not collected.")

        # Convert SetupDraft fields to OnboardDraft in the command layer
        # to avoid a Zone A → commands import violation (setup_state.py
        # must not import from src.commands).
        fields = draft.to_onboard_draft()
        onboard_draft = OnboardDraft(
            identity=fields[0],
            ado=fields[1],
            structure=fields[2],
            people=fields[3],
            style=fields[4],
        )
        edition_slug = generate_edition_slug(identity.program_name)

        if preview:
            _run_preview(draft, workstreams, output_dir=workspace, no_open=no_open)
            sm.transition("done")
            raise typer.Exit(code=0)

        if dry_run:
            typer.echo("\n  [dry-run] Files would be created — skipping write.\n")
            _show_next_steps(edition_slug, identity.program_id)
            sm.transition("done")
            raise typer.Exit(code=0)

        reports_root = workspace / "reports"
        paths = _resolve_onboard_paths(
            edition_name=edition_slug,
            program_id=identity.program_id,
            reports_root=reports_root,
        )
        documents = _build_documents(edition_slug, onboard_draft)
        _finalize_onboarding(
            edition_name=edition_slug,
            paths=paths,
            documents=documents,
            draft=onboard_draft,
        )

        sm.transition("done")

        file_count = len(dataclass_fields(documents))
        typer.echo(f"\n  Created {file_count} configuration files.")
        _show_next_steps(edition_slug, identity.program_id)

    except KeyboardInterrupt:
        typer.echo("\n\n  Setup cancelled (Ctrl-C).")
        raise typer.Exit(code=1)
