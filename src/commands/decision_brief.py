from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import webbrowser

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape
import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.commands.report_output import _write_output_text
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.decision_brief_engine import DecisionBrief, build_decision_brief
from src.core.edition_resolver import resolve_edition_paths, get_program_output_dir
from src.core.exceptions import RenderError
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS
from src.core.models_v2 import SectionRevisionStatus
from src.core.section_proposal_store import load_proposals
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.store_factory import build_signal_store_for_program_id


TEMPLATES_ROOT = Path(__file__).resolve().parents[2] / "templates"


@dataclass(frozen=True, slots=True)
class DecisionBriefArtifacts:
    issue_number: int
    html_path: Path
    item_count: int
    ai_enriched: bool


def decision_brief_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the active issue."),
    ai: bool = typer.Option(False, "--ai/--no-ai", help="Run LLM-as-judge to generate recommendations per item."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the decision brief in the browser."),
) -> None:
    try:
        artifacts = generate_decision_brief(
            edition_name=edition,
            issue_number=issue,
            ai=ai,
            open_browser=open_browser,
        )
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    typer.echo(f"Decision brief generated for Issue {artifacts.issue_number:03d}.")
    typer.echo(f"Items: {artifacts.item_count}  |  AI-enriched: {'yes' if artifacts.ai_enriched else 'no'}")
    typer.echo(f"Brief HTML: {artifacts.html_path}")
    raise typer.Exit(code=0)


def generate_decision_brief(
    *,
    edition_name: str,
    issue_number: int | None = None,
    ai: bool = False,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    open_browser: bool = False,
    create_ai_client: object = None,
) -> DecisionBriefArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=resolved_reports_root.parent / "programs",
    )
    if resolved_paths is None:
        raise typer.BadParameter(f"Unknown edition '{edition_name}'.")

    resolved_issue_number = (
        issue_number
        if issue_number is not None
        else _resolve_default_issue_number(
            edition_name=edition_name,
            program_id=resolved_paths.program_id,
            reports_root=resolved_reports_root,
            archive_root=resolved_archive_root,
        )
    )

    all_proposals = load_proposals(
        resolved_paths.program_id,
        resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
    )
    pending = tuple(p for p in all_proposals if p.status == SectionRevisionStatus.PENDING)
    if not pending:
        raise typer.BadParameter(
            f"No pending proposals for Issue {resolved_issue_number:03d}. "
            f"Run `vertex propose --edition {edition_name}` first."
        )

    signal_store = build_signal_store_for_program_id(
        resolved_paths.program_id,
        programs_root=resolved_reports_root.parent / "programs",
    )
    signal_map = {s.id: s for s in signal_store.read(resolved_paths.program_id)}

    brief = build_decision_brief(
        proposals=tuple(all_proposals),
        signal_map=signal_map,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        generated_at=datetime.now(),
    )

    if ai:
        brief = _enrich_with_ai(brief=brief, bundle=bundle, create_ai_client=create_ai_client)

    html = _render_decision_brief_html(brief=brief, edition_name=edition_name)
    target_path = _write_output_text(
        get_program_output_dir(edition_name, programs_root=resolved_reports_root.parent / 'programs') / "review" / "decision_brief.html",
        html,
    )
    if open_browser:
        webbrowser.open(target_path.resolve().as_uri())
    return DecisionBriefArtifacts(
        issue_number=resolved_issue_number,
        html_path=target_path,
        item_count=len(brief.items),
        ai_enriched=brief.ai_enriched,
    )


def _enrich_with_ai(
    *,
    brief: DecisionBrief,
    bundle: object,
    create_ai_client: object,
) -> DecisionBrief:
    if get_ai_mode() == AIMode.DISABLED:
        return brief
    from src.commands.report import _create_ai_client as default_create_ai_client
    from src.ai.decision_brief_advisor import advise_on_decision_brief

    client_factory = create_ai_client or default_create_ai_client
    try:
        client = client_factory(bundle)  # type: ignore[operator]
    except Exception:
        return brief
    if client is None:
        return brief
    try:
        return advise_on_decision_brief(client=client, brief=brief)
    except Exception:
        return brief


def _render_decision_brief_html(*, brief: DecisionBrief, edition_name: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)
    try:
        template = environment.get_template("decision_brief.j2")
    except TemplateNotFound as exc:
        raise RenderError("Missing template: decision_brief.j2") from exc
    return (
        template.render(
            title=f"{edition_name} decision brief",
            subtitle=(
                f"Issue {brief.issue_number:03d} — {brief.total_pending} pending decision(s). "
                + ("AI recommendations included." if brief.ai_enriched else "Run with --ai to add recommendations.")
            ),
            brief=brief,
            edition_name=edition_name,
            generated_at=brief.generated_at,
        ).strip()
        + "\n"
    )


def _resolve_default_issue_number(
    *,
    edition_name: str,
    program_id: str,
    reports_root: Path,
    archive_root: Path,
) -> int:
    from src.core.archive_store import find_latest_confirmed_entry, read_archive_index

    index = read_archive_index(edition_name, archive_root=archive_root)
    latest = find_latest_confirmed_entry(index)
    if latest is None:
        raise typer.BadParameter(
            f"No confirmed issues found for program '{program_id}'."
        )
    return latest.issue_number + 1
