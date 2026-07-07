from __future__ import annotations

import glob
import inspect
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.backfill_extractor import BackfillExtractor, BackfillExtractorError, ExtractedNewsletterIssue
from src.ai.discovery.lt_deck_extractor import LTDeckExtractorError, extract_lt_deck_candidates_from_pptx
from src.ai.discovery.newsletter_extractor import extract_newsletter_issue_number, extract_newsletter_publication_date
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.core.backfill_loader import BackfillPlan, get_backfill_config_path, get_backfill_plan_path
from src.core.backfill_loader import load_backfill_config_for_edition, load_backfill_plan_for_edition
from src.core.config_loader import load_bundle
from src.core.edition_resolver import get_program_output_dir, load_program, resolve_edition_paths
from src.core.exceptions import VertexError, StateError
from src.core.ledger.source_refs import LTDeckRef, NewsletterRef, source_ref_to_dict
from src.m365.agency_bridge import AgencyBridge
from src.m365.backfill_m365 import DiscoveredM365Source, M365Backfiller


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
_VALID_SOURCE_MODES = {"auto", "offline", "m365", "hybrid"}
_CATEGORY_ORDER = (
    "lt_decks",
    "acme_newsletters",
    "contoso_newsletters",
    "contoso_daily",
    "prior_emails",
    "prior_md",
    "transcripts",
    "chats",
    "reviews",
    "newsletters",
    "feedback",
    "meetings",
    "people_intelligence",
)

M365BackfillerFactory = Callable[[], M365Backfiller]
BackfillExtractorFactory = Callable[[], BackfillExtractor]


@dataclass(frozen=True, slots=True)
class DiscoveredBackfillItem:
    label: str
    reference: str
    source_id: str | None
    permalink: str | None
    origin: str


@dataclass(frozen=True, slots=True)
class BackfillCategorySummary:
    category: str
    count: int
    items: tuple[DiscoveredBackfillItem, ...]


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    program_id: str
    edition_name: str
    source_mode: str
    discovered_at: datetime
    since: str | None
    total_sources: int
    categories: tuple[BackfillCategorySummary, ...]
    newsletter_source_categories: tuple[str, ...]
    newsletter_extraction: BackfillExtractionSummary | None
    newsletter_candidate_export: BackfillCandidateExportSummary | None
    lt_deck_candidate_export: BackfillCandidateExportSummary | None
    warnings: tuple[str, ...]
    plan_path: str
    config_path: str | None
    output_dir: str


@dataclass(frozen=True, slots=True)
class BackfillArtifacts:
    summary: BackfillSummary
    preview_text: str


@dataclass(frozen=True, slots=True)
class BackfillExtractionSummary:
    processed_files: int
    extracted_issues: tuple[ExtractedNewsletterIssue, ...]
    scorecard_dimension_count: int
    workstream_blurb_count: int
    executive_summary_sample_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackfillCandidateExportSummary:
    output_path: str
    candidate_count: int
    event_counts: dict[str, int]
    warnings: tuple[str, ...]


def backfill_command(
    edition: str = typer.Option("", "--edition", help="Edition used for the backfill run (e.g. myprogram_weekly)."),
    source: str = typer.Option("auto", "--source", help="Backfill source mode: auto, offline, m365, or hybrid."),
    since: str | None = typer.Option(None, "--since", help="Optional ISO date filter (YYYY-MM-DD) for source discovery."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview discovered backfill sources without writing summary artifacts."),
) -> None:
    if source not in _VALID_SOURCE_MODES:
        supported = ", ".join(sorted(_VALID_SOURCE_MODES))
        raise typer.BadParameter(f"--source must be one of {supported}.")

    try:
        artifacts = build_backfill_artifacts(
            edition_name=edition,
            requested_source_mode=source,
            since=since,
            reports_root=REPORTS_ROOT,
            repo_root=REPO_ROOT,
        )
        typer.echo(artifacts.preview_text)

        if artifacts.summary.total_sources == 0:
            typer.echo("No backfill sources discovered.")
            raise typer.Exit(code=0)

        if dry_run:
            typer.echo("Dry run: no backfill summary written.")
            raise typer.Exit(code=0)

        if not typer.confirm(f"Process {artifacts.summary.total_sources} discovered source(s)?", default=True):
            raise typer.Exit(code=1)

        summary_md_path, summary_json_path = write_backfill_summary(artifacts.summary, repo_root=REPO_ROOT)
        typer.echo(f"Backfill summary: {summary_md_path}")
        typer.echo(f"Backfill data: {summary_json_path}")
        raise typer.Exit(code=0)
    except VertexError as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)


def build_backfill_artifacts(
    *,
    edition_name: str,
    requested_source_mode: str,
    since: str | None,
    reports_root: Path,
    repo_root: Path,
    m365_backfiller_factory: M365BackfillerFactory | None = None,
    newsletter_extractor_factory: BackfillExtractorFactory | None = None,
) -> BackfillArtifacts:
    bundle = load_bundle(
        edition_name,
        reports_root=reports_root,
        programs_root=repo_root / "programs",
    )
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=repo_root / "programs",
    )
    if resolved_paths is None:
        raise StateError(f"Unknown edition: {edition_name}")
    program = load_program(resolved_paths.program_id, programs_root=repo_root / "programs")
    if program is None:
        raise StateError(f"Program {resolved_paths.program_id} not found.")
    plan = load_backfill_plan_for_edition(edition_name, repo_root=repo_root)
    if plan is None:
        raise StateError(f"Missing backfill.yaml for {edition_name}. Create {get_backfill_plan_path(edition_name, repo_root=repo_root)} first.")

    backfill_config = load_backfill_config_for_edition(edition_name, repo_root=repo_root)
    since_date = _resolve_since_date(since, backfill_max_days=program.backfill_max_days)
    resolved_mode, warnings = _resolve_source_mode(
        requested_source_mode=requested_source_mode,
        m365_enabled=bundle.config.m365.enabled,
        backfill_config_present=backfill_config is not None,
        configured_strategy=backfill_config.newsletters.search_strategy if backfill_config is not None else None,
    )

    categories: list[BackfillCategorySummary] = []
    if resolved_mode in {"offline", "hybrid"}:
        categories.extend(_discover_offline_categories(plan=plan, repo_root=repo_root, since=since_date))

    if resolved_mode in {"m365", "hybrid"}:
        if backfill_config is None:
            raise StateError(
                f"Missing backfill_config.yaml for {edition_name}. Create {get_backfill_config_path(edition_name, repo_root=repo_root)} or use '--source offline'."
            )
        m365_backfiller = (m365_backfiller_factory or _build_m365_backfiller)()
        discoveries = m365_backfiller.discover_all(backfill_config, since=since_date)
        categories.extend(_discover_m365_categories(discoveries))

    ordered_categories = _order_categories(categories)
    total_sources = sum(category.count for category in ordered_categories)
    newsletter_extraction, extraction_warnings = _extract_offline_newsletters(
        edition_name=edition_name,
        categories=ordered_categories,
        repo_root=repo_root,
        newsletter_extractor_factory=newsletter_extractor_factory,
        newsletter_source_categories=frozenset(plan.newsletter_source_categories),
    )
    output_dir = _resolve_output_dir(plan=plan, repo_root=repo_root, edition_name=edition_name)
    newsletter_candidate_export = _summarize_newsletter_candidate_export(
        categories=ordered_categories,
        extraction=newsletter_extraction,
        output_dir=output_dir,
        repo_root=repo_root,
        newsletter_source_categories=frozenset(plan.newsletter_source_categories),
    )
    lt_deck_candidate_export = _summarize_lt_deck_candidate_export(
        ordered_categories,
        program_id=resolved_paths.program_id,
        output_dir=output_dir,
        repo_root=repo_root,
    )
    _newsletter_cats_tuple = tuple(plan.newsletter_source_categories)
    summary = BackfillSummary(
        program_id=resolved_paths.program_id,
        edition_name=edition_name,
        source_mode=resolved_mode,
        discovered_at=datetime.now(timezone.utc),
        since=since_date.isoformat() if since_date is not None else None,
        total_sources=total_sources,
        categories=tuple(ordered_categories),
        newsletter_source_categories=_newsletter_cats_tuple,
        newsletter_extraction=newsletter_extraction,
        newsletter_candidate_export=newsletter_candidate_export,
        lt_deck_candidate_export=lt_deck_candidate_export,
        warnings=(
            tuple(warnings)
            + tuple(extraction_warnings)
            + (newsletter_candidate_export.warnings if newsletter_candidate_export is not None else ())
            + (lt_deck_candidate_export.warnings if lt_deck_candidate_export is not None else ())
        ),
        plan_path=str(get_backfill_plan_path(edition_name, repo_root=repo_root)),
        config_path=(str(get_backfill_config_path(edition_name, repo_root=repo_root)) if backfill_config is not None else None),
        output_dir=str(output_dir),
    )
    return BackfillArtifacts(summary=summary, preview_text=render_backfill_preview(summary))


def write_backfill_summary(summary: BackfillSummary, *, repo_root: Path) -> tuple[Path, Path]:
    output_dir = Path(summary.output_dir)
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if summary.newsletter_candidate_export is not None:
        _write_newsletter_candidate_export(
            summary=summary,
            output_path=Path(summary.newsletter_candidate_export.output_path),
            repo_root=repo_root,
        )
    if summary.lt_deck_candidate_export is not None:
        _write_lt_deck_candidate_export(
            summary=summary,
            output_path=Path(summary.lt_deck_candidate_export.output_path),
            repo_root=repo_root,
        )
    summary_md_path = output_dir / "backfill.summary.md"
    summary_json_path = output_dir / "backfill.summary.json"
    summary_md_path.write_text(render_backfill_markdown(summary), encoding="utf-8")
    summary_json_path.write_text(json.dumps(asdict(summary), indent=2, default=_json_default) + "\n", encoding="utf-8")
    return summary_md_path, summary_json_path


def render_backfill_preview(summary: BackfillSummary) -> str:
    lines = [
        f"VERTEX BACKFILL — {_edition_label(summary.edition_name)}",
        "=" * (20 + len(_edition_label(summary.edition_name))),
        f"Program: {summary.program_id}",
        f"Source mode: {summary.source_mode}",
    ]
    if summary.config_path is not None:
        lines.append("Reading backfill_config.yaml...")
    if summary.since is not None:
        lines.append(f"Since: {summary.since}")
    lines.append("")

    if not summary.categories:
        lines.append("No sources matched the configured backfill inputs.")
    else:
        lines.append("Discovered sources:")
        for category in summary.categories:
            lines.append(f"- {category.category}: {category.count}")
            for item in category.items[:3]:
                lines.append(f"  - {item.label}")
            if category.count > 3:
                lines.append(f"  - ... {category.count - 3} more")

    if summary.newsletter_extraction is not None:
        lines.append("")
        lines.append("Newsletter extraction:")
        lines.append(f"- processed_files: {summary.newsletter_extraction.processed_files}")
        lines.append(f"- extracted_issues: {len(summary.newsletter_extraction.extracted_issues)}")
        lines.append(f"- scorecard_dimensions: {summary.newsletter_extraction.scorecard_dimension_count}")
        lines.append(f"- workstream_blurbs: {summary.newsletter_extraction.workstream_blurb_count}")
        if summary.newsletter_candidate_export is not None:
            lines.append(f"- candidate_export_rows: {summary.newsletter_candidate_export.candidate_count}")
            lines.append(
                "  - next_step: "
                + _newsletter_candidate_import_command(summary)
            )
        for issue in summary.newsletter_extraction.extracted_issues[:3]:
            issue_label = f"Issue {issue.issue_number:03d}" if issue.issue_number is not None else Path(issue.source_path).name
            if issue.title:
                issue_label = f"{issue_label} · {issue.title}"
            lines.append(f"  - {issue_label}")
    elif summary.newsletter_candidate_export is not None:
        lines.append("")
        lines.append("Newsletter candidate export:")
        lines.append(f"- candidate_export_rows: {summary.newsletter_candidate_export.candidate_count}")
        lines.append(
            "  - next_step: "
            + _newsletter_candidate_import_command(summary)
        )

    if summary.lt_deck_candidate_export is not None:
        lines.append("")
        lines.append("LT deck candidate export:")
        lines.append(f"- candidate_export_rows: {summary.lt_deck_candidate_export.candidate_count}")
        lines.append(
            "  - next_step: "
            + _lt_deck_candidate_import_command(summary)
        )

    if summary.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in summary.warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def render_backfill_markdown(summary: BackfillSummary) -> str:
    lines = [
        f"# VERTEX BACKFILL — {_edition_label(summary.edition_name)}",
        "",
        f"- Source mode: `{summary.source_mode}`",
        f"- Program: `{summary.program_id}`",
        f"- Discovered at: `{summary.discovered_at.isoformat()}`",
        f"- Total sources: `{summary.total_sources}`",
        f"- backfill.yaml: `{summary.plan_path}`",
    ]
    if summary.config_path is not None:
        lines.append(f"- backfill_config.yaml: `{summary.config_path}`")
    if summary.since is not None:
        lines.append(f"- Since: `{summary.since}`")
    for category in summary.categories:
        lines.extend(
            [
                "",
                f"## {category.category}",
                f"Count: {category.count}",
            ]
        )
        for item in category.items:
            detail = item.reference
            if item.permalink is not None:
                detail = f"{detail} | {item.permalink}"
            lines.append(f"- {item.label} ({detail})")
    if summary.newsletter_extraction is not None:
        lines.extend(
            [
                "",
                "## Newsletter extraction",
                f"- Processed files: `{summary.newsletter_extraction.processed_files}`",
                f"- Extracted issues: `{len(summary.newsletter_extraction.extracted_issues)}`",
                f"- Scorecard dimensions: `{summary.newsletter_extraction.scorecard_dimension_count}`",
                f"- Workstream blurbs: `{summary.newsletter_extraction.workstream_blurb_count}`",
                f"- Executive summary samples: `{summary.newsletter_extraction.executive_summary_sample_count}`",
            ]
        )
        if summary.newsletter_candidate_export is not None:
            lines.append(f"- Candidate export rows: `{summary.newsletter_candidate_export.candidate_count}`")
            lines.append(f"- Candidate export path: `{summary.newsletter_candidate_export.output_path}`")
            lines.append(f"- Next governed import step: `{_newsletter_candidate_import_command(summary)}`")
        for issue in summary.newsletter_extraction.extracted_issues:
            issue_label = f"Issue {issue.issue_number:03d}" if issue.issue_number is not None else Path(issue.source_path).name
            lines.extend(
                [
                    "",
                    f"### {issue_label}",
                    f"- Source: `{issue.source_path}`",
                    f"- Date: `{issue.issue_date or 'unknown'}`",
                    f"- Edition type: `{issue.edition_type or 'unknown'}`",
                    f"- Scorecard dimensions: `{len(issue.scorecard_dimensions)}`",
                    f"- Workstream blurbs: `{len(issue.workstream_blurbs)}`",
                ]
            )
            if issue.executive_summary:
                lines.append(f"- Executive summary: {issue.executive_summary}")
        if summary.newsletter_extraction.warnings:
            lines.append("")
            lines.append("### Extraction warnings")
            for warning in summary.newsletter_extraction.warnings:
                lines.append(f"- {warning}")
        if summary.newsletter_candidate_export is not None and summary.newsletter_candidate_export.warnings:
            lines.append("")
            lines.append("### Candidate export warnings")
            for warning in summary.newsletter_candidate_export.warnings:
                lines.append(f"- {warning}")
    elif summary.newsletter_candidate_export is not None:
        lines.extend(
            [
                "",
                "## Newsletter candidate export",
                f"- Candidate export rows: `{summary.newsletter_candidate_export.candidate_count}`",
                f"- Candidate export path: `{summary.newsletter_candidate_export.output_path}`",
                f"- Next governed import step: `{_newsletter_candidate_import_command(summary)}`",
            ]
        )
        if summary.newsletter_candidate_export.warnings:
            lines.append("")
            lines.append("### Candidate export warnings")
            for warning in summary.newsletter_candidate_export.warnings:
                lines.append(f"- {warning}")
    if summary.lt_deck_candidate_export is not None:
        lines.extend(
            [
                "",
                "## LT deck candidate export",
                f"- Candidate export rows: `{summary.lt_deck_candidate_export.candidate_count}`",
                f"- Candidate export path: `{summary.lt_deck_candidate_export.output_path}`",
                f"- Next governed import step: `{_lt_deck_candidate_import_command(summary)}`",
            ]
        )
        if summary.lt_deck_candidate_export.warnings:
            lines.append("")
            lines.append("### Candidate export warnings")
            for warning in summary.lt_deck_candidate_export.warnings:
                lines.append(f"- {warning}")
    if summary.warnings:
        lines.extend(["", "## Warnings"])
        for warning in summary.warnings:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _discover_offline_categories(
    *,
    plan: BackfillPlan,
    repo_root: Path,
    since: date | None,
) -> list[BackfillCategorySummary]:
    categories: list[BackfillCategorySummary] = []
    for source in plan.sources:
        matched_paths = sorted(
            Path(path)
            for path in glob.glob(str(repo_root / source.glob), recursive=True)
            if Path(path).is_file()
        )
        if since is not None:
            matched_paths = [path for path in matched_paths if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date() >= since]
        items = tuple(
            DiscoveredBackfillItem(
                label=path.name,
                reference=_relative_to_root(path, repo_root),
                source_id=_relative_to_root(path, repo_root),
                permalink=None,
                origin=source.kind,
            )
            for path in matched_paths
        )
        categories.append(
            BackfillCategorySummary(
                category=source.kind,
                count=len(items),
                items=items,
            )
        )
    return categories


def _discover_m365_categories(
    discoveries: dict[str, tuple[DiscoveredM365Source, ...]],
) -> list[BackfillCategorySummary]:
    categories: list[BackfillCategorySummary] = []
    for category_name, records in discoveries.items():
        items = tuple(
            DiscoveredBackfillItem(
                label=record.label,
                reference=record.question,
                source_id=record.source_id,
                permalink=record.permalink,
                origin=category_name,
            )
            for record in records
        )
        categories.append(
            BackfillCategorySummary(
                category=category_name,
                count=len(items),
                items=items,
            )
        )
    return categories


def _resolve_source_mode(
    *,
    requested_source_mode: str,
    m365_enabled: bool,
    backfill_config_present: bool,
    configured_strategy: str | None,
) -> tuple[str, tuple[str, ...]]:
    warnings: list[str] = []
    if requested_source_mode == "auto":
        if configured_strategy in {"m365", "hybrid"} and m365_enabled and backfill_config_present:
            return configured_strategy, ()
        if configured_strategy in {"m365", "hybrid"} and not m365_enabled:
            warnings.append("M365 discovery is disabled in config.yaml; using offline discovery only.")
        if configured_strategy in {"m365", "hybrid"} and not backfill_config_present:
            warnings.append("backfill_config.yaml is missing; using offline discovery only.")
        return "offline", tuple(warnings)

    if requested_source_mode in {"m365", "hybrid"} and not m365_enabled:
        if requested_source_mode == "hybrid":
            return "offline", ("M365 discovery is disabled in config.yaml; using offline discovery only.",)
        raise StateError("M365 backfill is disabled in config.yaml. Enable m365.enabled or use '--source offline'.")
    return requested_source_mode, ()


def _resolve_output_dir(*, plan: BackfillPlan, repo_root: Path, edition_name: str) -> Path:
    if plan.output is not None:
        configured = Path(plan.output)
        return configured if configured.is_absolute() else repo_root / configured
    return get_program_output_dir(edition_name, programs_root=repo_root / "programs") / "backfill"


def _build_m365_backfiller() -> M365Backfiller:
    return M365Backfiller(AgencyBridge())


def _build_backfill_extractor(*, trace_context: AITraceContext | None = None) -> BackfillExtractor:
    # D-20: bind the trace context to the process-level ContextVar so any
    # nested helper (rate-limit scope, cost-guard construction, trace-file
    # write path) that doesn't take an explicit `trace_context=` arg still
    # picks it up. The explicit kwarg below still wins, so this is
    # behavior-preserving.
    with use_trace_context(trace_context):
        return BackfillExtractor.from_environment(trace_context=trace_context)


def _resolve_since_date(since: str | None, *, backfill_max_days: int) -> date | None:
    if since is None:
        return date.fromordinal(date.today().toordinal() - backfill_max_days)
    try:
        return date.fromisoformat(since)
    except ValueError as exc:
        raise typer.BadParameter("--since must be an ISO date in YYYY-MM-DD format.") from exc


def _edition_label(edition_name: str) -> str:
    return edition_name.replace("_", " ").title()


def _relative_to_root(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _order_categories(categories: list[BackfillCategorySummary]) -> list[BackfillCategorySummary]:
    ranking = {name: index for index, name in enumerate(_CATEGORY_ORDER)}
    return sorted(categories, key=lambda category: (ranking.get(category.category, len(ranking)), category.category))


def _extract_offline_newsletters(
    *,
    edition_name: str,
    categories: list[BackfillCategorySummary],
    repo_root: Path,
    newsletter_extractor_factory: BackfillExtractorFactory | None,
    newsletter_source_categories: frozenset[str],
) -> tuple[BackfillExtractionSummary | None, tuple[str, ...]]:
    newsletter_paths = tuple(
        repo_root / item.reference
        for category in categories
        if category.category in newsletter_source_categories
        for item in category.items
    )
    if not newsletter_paths:
        return None, ()
    newsletter_paths = _dedupe_newsletter_source_paths(newsletter_paths)
    if get_ai_mode() == AIMode.DISABLED:
        return None, ("Newsletter extraction skipped: invocation AI is disabled by --no-ai / AIMode.DISABLED.",)

    try:
        if newsletter_extractor_factory is None:
            extractor = _build_default_backfill_extractor(
                trace_context=_build_backfill_trace_context(edition_name=edition_name),
            )
        else:
            extractor = newsletter_extractor_factory()
    except BackfillExtractorError as error:
        return None, (f"Newsletter extraction skipped: {error}",)

    try:
        extracted_issues = extractor.extract_newsletters(newsletter_paths)
    except BackfillExtractorError as error:
        return None, (f"Newsletter extraction skipped: {error}",)

    return (
        BackfillExtractionSummary(
            processed_files=len(newsletter_paths),
            extracted_issues=extracted_issues,
            scorecard_dimension_count=sum(len(issue.scorecard_dimensions) for issue in extracted_issues),
            workstream_blurb_count=sum(len(issue.workstream_blurbs) for issue in extracted_issues),
            executive_summary_sample_count=sum(len(issue.style_sample.executive_summary_paragraphs) for issue in extracted_issues),
            warnings=(),
        ),
        (),
    )


def _dedupe_newsletter_source_paths(source_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    selected: dict[str, Path] = {}
    for path in sorted(source_paths, key=lambda item: item.as_posix().lower()):
        key = _newsletter_source_dedupe_key(path)
        current = selected.get(key)
        if current is None or _newsletter_source_rank(path) < _newsletter_source_rank(current):
            selected[key] = path
    return tuple(sorted(selected.values(), key=lambda item: item.as_posix().lower()))


def _newsletter_source_dedupe_key(path: Path) -> str:
    normalized = re.sub(r"^(re|fw|fwd)[_:\s-]+", "", path.stem.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"^\[for review\][_\s-]*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+-\s+[A-Za-z][^-]*(?:\s+-\s+outlook)?$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    issue_match = re.search(r"\bissue\s*_?\s*(\d+)\b", normalized, flags=re.IGNORECASE)
    if issue_match is not None:
        return f"issue:{int(issue_match.group(1)):03d}"
    date_match = re.search(r"\b(\d{2}[_-]\d{2}[_-]\d{4})\b", normalized)
    if date_match is not None:
        return f"{normalized[:80]}|date:{date_match.group(1)}"
    return normalized[:160]


def _newsletter_source_rank(path: Path) -> tuple[int, int, int, int]:
    stem = path.stem.lower()
    prefix_penalty = 0 if not re.match(r"^(re|fw|fwd)[_:\s-]+", stem, flags=re.IGNORECASE) else 1
    review_penalty = 1 if "[for review]" in stem else 0
    suffix_rank = 0 if path.suffix.lower() == ".eml" else 1
    length_rank = len(path.name)
    return (prefix_penalty, review_penalty, suffix_rank, length_rank)


def _build_backfill_trace_context(*, edition_name: str) -> AITraceContext:
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=edition_name,
        run_id=f"{edition_name}:backfill:newsletter:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.backfill._extract_offline_newsletters",
        metadata={
            "edition_name": edition_name,
            "task_type": "newsletter_backfill_extraction",
            "run_budget_usd": 0.5,
        },
    )


def _build_default_backfill_extractor(*, trace_context: AITraceContext) -> BackfillExtractor:
    if get_ai_mode() == AIMode.DISABLED:
        return BackfillExtractor(client=None)
    if "trace_context" in inspect.signature(_build_backfill_extractor).parameters:
        return _build_backfill_extractor(trace_context=trace_context)
    return _build_backfill_extractor()


def _summarize_newsletter_candidate_export(
    *,
    categories: list[BackfillCategorySummary],
    extraction: BackfillExtractionSummary | None,
    output_dir: Path,
    repo_root: Path,
    newsletter_source_categories: frozenset[str],
) -> BackfillCandidateExportSummary | None:
    rows, warnings = _build_newsletter_candidate_export_rows(
        categories=categories,
        extracted_issues=extraction.extracted_issues if extraction is not None else (),
        repo_root=repo_root,
        newsletter_source_categories=newsletter_source_categories,
    )
    if not rows and not warnings:
        return None
    event_counts: dict[str, int] = {}
    for row in rows:
        event_type = str(row["proposed_event_type"])
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    return BackfillCandidateExportSummary(
        output_path=str(output_dir / "newsletter.discovery_import.jsonl"),
        candidate_count=len(rows),
        event_counts=event_counts,
        warnings=tuple(warnings),
    )


def _summarize_lt_deck_candidate_export(
    categories: list[BackfillCategorySummary],
    *,
    program_id: str,
    output_dir: Path,
    repo_root: Path,
) -> BackfillCandidateExportSummary | None:
    rows, warnings = _build_lt_deck_candidate_export_rows(categories, program_id=program_id, repo_root=repo_root)
    if not rows and not warnings:
        return None
    event_counts: dict[str, int] = {}
    for row in rows:
        event_type = str(row["proposed_event_type"])
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    return BackfillCandidateExportSummary(
        output_path=str(output_dir / "lt_deck.discovery_import.jsonl"),
        candidate_count=len(rows),
        event_counts=event_counts,
        warnings=tuple(warnings),
    )


def _write_newsletter_candidate_export(
    *,
    summary: BackfillSummary,
    output_path: Path,
    repo_root: Path,
) -> None:
    rows, _warnings = _build_newsletter_candidate_export_rows(
        categories=list(summary.categories),
        extracted_issues=summary.newsletter_extraction.extracted_issues if summary.newsletter_extraction is not None else (),
        repo_root=repo_root,
        newsletter_source_categories=frozenset(summary.newsletter_source_categories),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_lt_deck_candidate_export(
    *,
    summary: BackfillSummary,
    output_path: Path,
    repo_root: Path,
) -> None:
    rows, _warnings = _build_lt_deck_candidate_export_rows(
        list(summary.categories),
        program_id=summary.program_id,
        repo_root=repo_root,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _build_newsletter_candidate_export_rows(
    *,
    categories: list[BackfillCategorySummary],
    extracted_issues: tuple[ExtractedNewsletterIssue, ...],
    repo_root: Path,
    newsletter_source_categories: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    extracted_by_source = {Path(issue.source_path).resolve(): issue for issue in extracted_issues}
    newsletter_paths = _dedupe_newsletter_source_paths(
        tuple(
            repo_root / item.reference
            for category in categories
            if category.category in newsletter_source_categories
            for item in category.items
        )
    )
    for path in newsletter_paths:
        issue = extracted_by_source.get(path.resolve())
        if issue is None:
            issue_date = extract_newsletter_publication_date(path)
            if issue_date is None:
                warnings.append(f"Skipped {path.name}: publication date is missing or not parseable from filename.")
                continue
            issue_number = extract_newsletter_issue_number(path)
            source_ref = NewsletterRef(
                file_path=_relative_to_root(path, repo_root),
                publication_date=issue_date,
                issue_number=issue_number,
            )
            rows.append(
                _newsletter_artifact_candidate_row_from_source(
                    path=path,
                    issue_number=issue_number,
                    source_ref=source_ref,
                    occurred_at=issue_date,
                )
            )
            continue
        issue_date = _parse_issue_date(issue.issue_date)
        if issue_date is None:
            warnings.append(f"Skipped {Path(issue.source_path).name}: issue_date is missing or not ISO-8601.")
            continue
        source_ref = _newsletter_ref_for_issue(issue, repo_root=repo_root, issue_date=issue_date)
        rows.append(_newsletter_artifact_candidate_row(issue, source_ref=source_ref, occurred_at=issue_date))
        for dimension in issue.scorecard_dimensions:
            severity = _map_extracted_risk_to_severity(dimension.risk)
            if severity is None:
                continue
            rows.append(
                _newsletter_risk_candidate_row(
                    issue,
                    dimension_name=dimension.dimension_name,
                    severity=severity,
                    source_ref=source_ref,
                    occurred_at=issue_date,
                )
            )
    return rows, warnings


def _build_lt_deck_candidate_export_rows(
    categories: list[BackfillCategorySummary],
    *,
    program_id: str,
    repo_root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for category in categories:
        if category.category != "lt_decks":
            continue
        for item in category.items:
            path = repo_root / item.reference
            deck_date = _parse_lt_deck_date(path)
            if deck_date is None:
                warnings.append(f"Skipped {path.name}: LT deck filename does not contain a parseable date.")
                continue
            source_ref = LTDeckRef(
                file_path=_relative_to_root(path, repo_root),
                deck_date=deck_date,
            )
            rows.append(_lt_deck_artifact_candidate_row(source_ref=source_ref, title=path.stem.strip(), occurred_at=deck_date))
            try:
                extracted_batch = extract_lt_deck_candidates_from_pptx(
                    program_id=program_id,
                    source_path=path,
                    relative_path=source_ref.file_path,
                    batch_id="lt-deck-backfill-export",
                    pipeline="lt_deck_backfill",
                    continue_on_marker_errors=True,
                )
            except LTDeckExtractorError as error:
                warnings.append(f"Skipped structured LT deck extraction for {path.name}: {error}")
                continue
            rows.extend(_candidate_event_import_row(candidate) for candidate in extracted_batch.candidates)
            warnings.extend(
                f"{path.name} {warning}"
                for warning in extracted_batch.warnings
            )
    return rows, warnings


def _lt_deck_artifact_candidate_row(
    *,
    source_ref: LTDeckRef,
    title: str,
    occurred_at: date,
) -> dict[str, Any]:
    artifact_id = f"published_artifact:lt-deck:{occurred_at.isoformat()}:{_slugify(title)}"
    return {
        "proposed_event_type": "artifact.published.v1",
        "proposed_payload": {
            "artifact_id": artifact_id,
            "artifact_kind": "lt_deck",
            "title": title,
            "location": source_ref.file_path,
            "period_start": occurred_at.isoformat(),
            "period_end": occurred_at.isoformat(),
        },
        "proposed_occurred_at": datetime.combine(occurred_at, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "proposed_temporal_confidence": "approximate",
        "proposed_confidence": "source_authoritative",
        "source_ref": source_ref_to_dict(source_ref),
        "pipeline": "lt_deck_backfill",
        "extraction_confidence": 0.95,
        "entity_resolution": [
            {
                "raw_name": artifact_id,
                "resolved_entity_id": artifact_id,
                "match_kind": "imported",
                "score": 1.0,
            }
        ],
        "corroborating_refs": [],
    }


def _candidate_event_import_row(candidate: Any) -> dict[str, Any]:
    return {
        "proposed_event_type": candidate.proposed_event_type,
        "proposed_payload": candidate.proposed_payload,
        "proposed_occurred_at": candidate.proposed_occurred_at.isoformat(),
        "proposed_temporal_confidence": candidate.proposed_temporal_confidence,
        "proposed_confidence": candidate.proposed_confidence,
        "source_ref": source_ref_to_dict(candidate.source_ref),
        "pipeline": candidate.pipeline,
        "extraction_confidence": candidate.extraction_confidence,
        "entity_resolution": [
            {
                "raw_name": resolution.raw_name,
                "resolved_entity_id": resolution.resolved_entity_id,
                "match_kind": resolution.match_kind,
                "score": resolution.score,
            }
            for resolution in candidate.entity_resolution
        ],
        "dedupe_key": candidate.dedupe_key,
        "dedupe_core_hash": candidate.dedupe_core_hash,
        "source_document_key": candidate.source_document_key,
        "corroborating_refs": [source_ref_to_dict(ref) for ref in candidate.corroborating_refs],
    }


def _newsletter_ref_for_issue(
    issue: ExtractedNewsletterIssue,
    *,
    repo_root: Path,
    issue_date: date,
) -> NewsletterRef:
    return NewsletterRef(
        file_path=_relative_to_root(Path(issue.source_path), repo_root),
        publication_date=issue_date,
        issue_number=issue.issue_number,
    )


def _newsletter_artifact_candidate_row(
    issue: ExtractedNewsletterIssue,
    *,
    source_ref: NewsletterRef,
    occurred_at: date,
) -> dict[str, Any]:
    if issue.issue_number is not None:
        artifact_id = f"published_artifact:issue-{issue.issue_number:03d}"
        title = issue.title or f"Issue {issue.issue_number:03d}"
    else:
        slug = Path(issue.source_path).stem.strip().lower().replace(" ", "-")
        artifact_id = f"published_artifact:newsletter:{occurred_at.isoformat()}:{slug}"
        title = issue.title or Path(issue.source_path).stem.strip()
    return {
        "proposed_event_type": "artifact.published.v1",
        "proposed_payload": {
            "artifact_id": artifact_id,
            "artifact_kind": "newsletter",
            "title": title,
            "location": source_ref.file_path,
            "period_start": occurred_at.isoformat(),
            "period_end": occurred_at.isoformat(),
        },
        "proposed_occurred_at": datetime.combine(occurred_at, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "proposed_temporal_confidence": "exact",
        "proposed_confidence": "source_authoritative",
        "source_ref": source_ref_to_dict(source_ref),
        "pipeline": "newsletter_backfill",
        "extraction_confidence": 0.95,
        "entity_resolution": [
            {
                "raw_name": artifact_id,
                "resolved_entity_id": artifact_id,
                "match_kind": "imported",
                "score": 1.0,
            }
        ],
        "corroborating_refs": [],
    }


def _newsletter_artifact_candidate_row_from_source(
    *,
    path: Path,
    issue_number: int | None,
    source_ref: NewsletterRef,
    occurred_at: date,
) -> dict[str, Any]:
    if issue_number is not None:
        artifact_id = f"published_artifact:issue-{issue_number:03d}"
        title = f"Issue {issue_number:03d}"
    else:
        artifact_id = f"published_artifact:newsletter:{occurred_at.isoformat()}:{_slugify(path.stem)}"
        title = path.stem.strip()
    return {
        "proposed_event_type": "artifact.published.v1",
        "proposed_payload": {
            "artifact_id": artifact_id,
            "artifact_kind": "newsletter",
            "title": title,
            "location": source_ref.file_path,
            "period_start": occurred_at.isoformat(),
            "period_end": occurred_at.isoformat(),
        },
        "proposed_occurred_at": datetime.combine(occurred_at, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "proposed_temporal_confidence": "exact",
        "proposed_confidence": "source_authoritative",
        "source_ref": source_ref_to_dict(source_ref),
        "pipeline": "newsletter_backfill",
        "extraction_confidence": 0.95,
        "entity_resolution": [
            {
                "raw_name": artifact_id,
                "resolved_entity_id": artifact_id,
                "match_kind": "imported",
                "score": 1.0,
            }
        ],
        "corroborating_refs": [],
    }


def _newsletter_risk_candidate_row(
    issue: ExtractedNewsletterIssue,
    *,
    dimension_name: str,
    severity: str,
    source_ref: NewsletterRef,
    occurred_at: date,
) -> dict[str, Any]:
    issue_label = (
        f"issue-{issue.issue_number:03d}"
        if issue.issue_number is not None
        else Path(issue.source_path).stem.strip().lower().replace(" ", "-")
    )
    return {
        "proposed_event_type": "risk.raised.v1",
        "proposed_payload": {
            "risk_id": f"risk:newsletter:{issue_label}:{_slugify(dimension_name)}",
            "title": f"{dimension_name} risk surfaced in newsletter {issue_label}",
            "severity": severity,
            "description": (
                issue.executive_summary
                or f"Historical newsletter extraction flagged {dimension_name} at {severity} severity."
            ),
        },
        "proposed_occurred_at": datetime.combine(occurred_at, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "proposed_temporal_confidence": "exact",
        "proposed_confidence": "ai_extracted",
        "source_ref": source_ref_to_dict(source_ref),
        "pipeline": "newsletter_backfill",
        "extraction_confidence": 0.75,
        "entity_resolution": [],
        "corroborating_refs": [],
    }


def _parse_issue_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_lt_deck_date(path: Path) -> date | None:
    stem = path.stem
    for token in stem.replace("_", " ").split():
        normalized = token.strip().rstrip("-")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.date()
        for pattern in ("%Y%m%d", "%Y%m", "%Y%m-%d"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    return None


def _map_extracted_risk_to_severity(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"high", "blocked"}:
        return "high"
    if normalized == "medium":
        return "medium"
    return None


def _slugify(value: str) -> str:
    collapsed = "".join(character.lower() if character.isalnum() else "-" for character in value.strip())
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-") or "unknown"


def _newsletter_candidate_import_command(summary: BackfillSummary) -> str:
    if summary.newsletter_candidate_export is None:
        return ""
    return (
        f"vertex discover candidates --program {summary.program_id} "
        f"--source backfill_import --input-jsonl \"{summary.newsletter_candidate_export.output_path}\""
    )


def _lt_deck_candidate_import_command(summary: BackfillSummary) -> str:
    if summary.lt_deck_candidate_export is None:
        return ""
    return (
        f"vertex discover candidates --program {summary.program_id} "
        f"--source backfill_import --input-jsonl \"{summary.lt_deck_candidate_export.output_path}\""
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
