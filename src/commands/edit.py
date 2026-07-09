from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import typer

from src.commands.diff import _latest_draft_issue_number, _resolve_section
from src.commands.report import _build_chapter_templates, _build_exec_summary_template, _is_continuity_layout, _visible_continuity_chapters
from src.commands.report import generate_report_draft
from src.core.archive_store import is_issue_confirmed
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.models import EditionType
from src.core.jinja_filters import build_anchor
from src.core.narrative_store import get_narratives_dir
from src.core.snapshot_store import ARCHIVE_ROOT


EditorRunner = Callable[[Path, bool], bool]
PromptRunner = Callable[[str], bool]
RerunRunner = Callable[[str, int, Path, Path], None]


@dataclass(frozen=True, slots=True)
class EditResult:
    issue_number: int
    section_id: str
    path: Path
    read_only: bool
    created: bool
    opened_in_editor: bool
    reran_dry_run: bool


def edit_command(
    edition: str = typer.Option("", "--edition", help="Edition name."),
    section: str = typer.Option(..., "--section", help="Section id, dimension name, or exec_summary."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the latest draft issue."),
) -> None:
    result = run_edit(
        edition_name=edition,
        section=section,
        issue_number=issue,
        reports_root=REPORTS_ROOT,
        archive_root=ARCHIVE_ROOT,
        programs_root=PROGRAMS_ROOT,
    )
    if result.read_only:
        typer.echo(f"Read-only issue path: {result.path}")
    elif result.opened_in_editor:
        typer.echo(f"Opened: {result.path}")
    else:
        typer.echo(f"Editor unavailable. File path: {result.path}")
    if result.reran_dry_run:
        typer.echo("Re-ran vertex report --dry-run for immediate feedback.")
    raise typer.Exit(code=0)


def run_edit(
    *,
    edition_name: str,
    section: str,
    issue_number: int | None,
    reports_root: Path,
    archive_root: Path,
    programs_root: Path = PROGRAMS_ROOT,
    editor_runner: EditorRunner | None = None,
    prompt_runner: PromptRunner | None = None,
    rerun_runner: RerunRunner | None = None,
) -> EditResult:
    latest_issue_number = _latest_draft_issue_number(edition_name, programs_root=programs_root)
    resolved_issue_number = issue_number or latest_issue_number
    # A confirmed (published) issue is a permanent record — its narrative source
    # must never be reopened for editing, even if it happens to also be the
    # "latest draft" on disk (e.g. right after confirming). Check the archive
    # index, not just draft recency.
    read_only = (
        resolved_issue_number < latest_issue_number
        or is_issue_confirmed(edition_name, resolved_issue_number, archive_root=archive_root)
    )

    target_path, section_id, created = _prepare_edit_target(
        edition_name=edition_name,
        section=section,
        issue_number=resolved_issue_number,
        reports_root=reports_root,
        read_only=read_only,
    )

    opened_in_editor = False
    if not read_only:
        opened_in_editor = (editor_runner or _default_editor_runner)(target_path, read_only)

    reran_dry_run = False
    if not read_only and (prompt_runner or _default_prompt_runner)("Re-run --dry-run now?"):
        (rerun_runner or _default_rerun_runner)(edition_name, resolved_issue_number, reports_root, archive_root)
        reran_dry_run = True

    return EditResult(
        issue_number=resolved_issue_number,
        section_id=section_id,
        path=target_path,
        read_only=read_only,
        created=created,
        opened_in_editor=opened_in_editor,
        reran_dry_run=reran_dry_run,
    )


def _prepare_edit_target(
    *,
    edition_name: str,
    section: str,
    issue_number: int,
    reports_root: Path,
    read_only: bool = False,
) -> tuple[Path, str, bool]:
    narratives_dir = get_narratives_dir(edition_name, issue_number, reports_root=reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    section_id, filename, section_title = _resolve_edit_section_target(
        edition_name=edition_name,
        section=section,
        reports_root=reports_root,
        issue_number=issue_number,
    )
    target_path = narratives_dir / filename
    created = False
    if not target_path.exists() and not read_only:
        target_path.write_text(
            _scaffold_content(
                issue_number,
                section_id,
                section_title=section_title,
                edition_name=edition_name,
                reports_root=reports_root,
            ),
            encoding="utf-8",
        )
        created = True
    return target_path, section_id, created


def _resolve_edit_section_target(
    *,
    edition_name: str,
    section: str,
    reports_root: Path,
    issue_number: int,
) -> tuple[str, str, str | None]:
    if section.strip() == "exec_summary":
        return "exec_summary", "exec_summary.md", None
    bundle = load_bundle(edition_name, reports_root=reports_root)
    if _is_continuity_layout(bundle):
        edition_type = EditionType.from_string(bundle.config.edition.type)
        assert bundle.chapter_contract is not None
        alias_to_section: dict[str, str] = {}
        section_titles: dict[str, str] = {}
        for chapter in _visible_continuity_chapters(bundle, edition_type):
            section_titles[chapter.id] = chapter.title
            aliases = {
                chapter.id,
                f"chapter_{chapter.id}",
                build_anchor(chapter.title),
            }
            for dimension_id in chapter.dimensions:
                binding = bundle.chapter_contract.resolve_dimension(dimension_id)
                if binding is None:
                    continue
                aliases.add(build_anchor(binding[1]))
                aliases.add(build_anchor(f"{binding[0]}-{binding[1]}"))
            for alias in aliases:
                alias_to_section.setdefault(alias, chapter.id)
        normalized = section.strip()
        anchored = build_anchor(normalized)
        if normalized in alias_to_section:
            section_id = alias_to_section[normalized]
        elif anchored in alias_to_section:
            section_id = alias_to_section[anchored]
        else:
            matching_sections = {
                alias_to_section[alias]
                for alias in alias_to_section
                if alias == anchored or alias.endswith(f"-{anchored}")
            }
            if len(matching_sections) == 1:
                section_id = next(iter(matching_sections))
            elif not matching_sections:
                choices = ", ".join(["exec_summary", *sorted(alias_to_section)])
                raise typer.BadParameter(f"Unknown section '{section}'. Available sections: {choices}")
            else:
                choices = ", ".join(sorted(matching_sections))
                raise typer.BadParameter(f"Section '{section}' is ambiguous. Use one of: {choices}")
        return section_id, f"chapter_{section_id}.md", section_titles.get(section_id)
    available_section_ids = {
        build_anchor(f"{scorecard.name}-{dimension.name}")
        for scorecard in bundle.config.scorecards
        for dimension in scorecard.dimensions
    }
    section_id = _resolve_section(section, available_section_ids) or "exec_summary"
    filename = "exec_summary.md" if section_id == "exec_summary" else f"ws_{section_id}.md"
    return section_id, filename, None


def _scaffold_content(
    issue_number: int,
    section_id: str,
    *,
    section_title: str | None,
    edition_name: str,
    reports_root: Path,
) -> str:
    if section_id == "exec_summary":
        bundle = load_bundle(edition_name, reports_root=reports_root)
        return _build_exec_summary_template(issue_number, layout_mode=bundle.config.layout_mode)
    if section_title is not None:
        return "\n".join(
            [
                f"<!-- vertex:scaffold Issue {issue_number} - {section_title} -->",
                "<!-- vertex:scaffold Optional chapter summary or note. Leave empty to render the table only. -->",
                "",
            ]
        )
    title = section_id.replace("-", " ").replace("_", " ").title()
    return "\n".join(
        [
            f"<!-- vertex:scaffold Issue {issue_number} - {title} -->",
            "<!-- vertex:scaffold Editorial: Lead with the delta. Max 3 sentences, 60 words. -->",
            "",
            "[Your narrative here]",
            "",
        ]
    )


def _default_editor_runner(path: Path, read_only: bool) -> bool:
    if read_only:
        return False
    editor = os.getenv("EDITOR")
    if editor:
        command = [*shlex.split(editor, posix=False), str(path)]
        subprocess.run(command, check=False)
        return True
    if shutil.which("code"):
        subprocess.run(["code", "--wait", str(path)], check=False)
        return True
    return False


def _default_prompt_runner(message: str) -> bool:
    return typer.confirm(message, default=True)


def _default_rerun_runner(
    edition_name: str,
    issue_number: int,
    reports_root: Path,
    archive_root: Path,
) -> None:
    generate_report_draft(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )