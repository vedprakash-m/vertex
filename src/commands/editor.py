"""vertex editor — standalone editorial evaluation commands.

γ-Read Phase 5 (§18): The `editor report` command runs a full editorial
evaluation and produces a per-persona pass/fail summary without running
the full report pipeline.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
from pathlib import Path

import typer

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.config_loader import REPORTS_ROOT, load_editorial_rules, load_persona_registry
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.overrides_store import load_overrides
from src.core.persona_checker import run_persona_checks
from src.core.published_narrative_store import load_published_narratives

app = typer.Typer(help="Editorial evaluation commands.")


def _detect_issue_number(program_dir: Path) -> int | None:
    overrides_dir = program_dir / "overrides"
    if not overrides_dir.exists():
        return None
    issue_numbers: list[int] = []
    for path in overrides_dir.glob("issue_*.yaml"):
        suffix = path.stem.removeprefix("issue_")
        if suffix.isdigit():
            issue_numbers.append(int(suffix))
    return max(issue_numbers) if issue_numbers else None


def _load_published_baseline(edition: str, issue_number: int, *, archive_root: Path) -> dict[str, str] | None:
    latest_confirmed = find_latest_confirmed_entry(
        read_archive_index(edition, archive_root=archive_root),
        before_issue_number=issue_number,
    )
    if latest_confirmed is None:
        return None
    published = load_published_narratives(
        edition,
        latest_confirmed.issue_number,
        archive_root=archive_root,
    )
    if not published:
        return None
    baseline: dict[str, str] = {}
    for filename, text in published.items():
        if filename == "exec_summary.md":
            baseline["exec_summary"] = text
        elif filename.startswith("ws_") and filename.endswith(".md"):
            baseline[f"narrative:{filename[3:-3]}"] = text
    return baseline or None


@app.command("report")
def editor_report_command(
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    issue: int | None = typer.Option(None, "--issue", min=1, help="Issue number (defaults to current)."),
    output_format: str = typer.Option("human", "--format", help="Output format: human or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    reports_root: Path = typer.Option(REPORTS_ROOT, hidden=True),
) -> None:
    """Run standalone editorial evaluation and produce per-persona pass/fail summary.

    Exit codes:
      0 — no findings at or above warn (clean; only info)
      2 — one or more warn-severity findings, no blocking failure
      3 — at least one block-severity gate failed (after its enforce_after date)
    """
    resolved = resolve_edition_paths(edition, programs_root=programs_root)
    if resolved is None:
        raise typer.BadParameter(f"Unknown edition {edition!r}.")
    archive_root = reports_root.parent / "archive"

    issue_number = issue if issue is not None else _detect_issue_number(resolved.program_dir)
    if issue_number is None:
        latest_confirmed = find_latest_confirmed_entry(
            read_archive_index(resolved.edition_id, archive_root=archive_root)
        )
        issue_number = latest_confirmed.issue_number if latest_confirmed is not None else None

    rules_path = resolved.program_dir / "editorial_rules.yaml"
    personas_path = resolved.knowledge_dir / "personas.yaml"
    try:
        rules = load_editorial_rules(rules_path)
    except Exception as exc:
        typer.echo(f"Could not load editorial rules: {exc}", err=True)
        raise typer.Exit(code=1)

    try:
        registry = load_persona_registry(personas_path)
    except Exception as exc:
        typer.echo(f"Could not load persona registry: {exc}", err=True)
        raise typer.Exit(code=1)
    if registry is None:
        typer.echo(f"Could not load persona registry: no personas.yaml found at {personas_path}", err=True)
        raise typer.Exit(code=1)

    overrides = (
        load_overrides(
            resolved.edition_id,
            reports_root=reports_root,
            issue_number=issue_number,
        )
        if issue_number is not None
        else None
    )
    published_baseline = (
        _load_published_baseline(
            resolved.edition_id,
            issue_number,
            archive_root=archive_root,
        )
        if issue_number is not None
        else None
    )

    exec_summary_text = ""
    loaded_narratives: dict[str, str] = {}
    workstream_blurbs: dict[str, str] = {}
    if issue_number is not None:
        narratives_path = resolved.program_dir / "narratives" / f"issue_{issue_number:03d}"
        if narratives_path.exists():
            exec_summary_file = narratives_path / "exec_summary.md"
            if exec_summary_file.exists():
                exec_summary_text = exec_summary_file.read_text(encoding="utf-8")
            for md_file in sorted(narratives_path.glob("*.md")):
                if md_file.name == "exec_summary.md":
                    continue
                content = md_file.read_text(encoding="utf-8")
                loaded_narratives[md_file.stem] = content
                workstream_blurbs[md_file.stem] = content

    report = run_persona_checks(
        registry=registry,
        exec_summary_text=exec_summary_text,
        workstream_blurbs=workstream_blurbs,
        loaded_narratives=loaded_narratives,
        rendered_html="",
        subject_line="",
        ban_rule_results=(),
        structural_rule_results=(),
        editorial_rules=rules,
        overrides=overrides,
        program_phase=None,
        evaluation_date=date.today(),
        published_baseline=published_baseline,
    )
    if report is None:
        raise typer.Exit(code=0)

    results = report.results
    has_block = any(result.effective_severity == "block" and result.status == "failed" for result in results)
    has_warn = any(result.effective_severity in {"warn", "block"} and result.status == "failed" for result in results)
    exit_code = 3 if has_block else (2 if has_warn else 0)

    if output_format == "json":
        typer.echo(json.dumps([
            {
                "persona_id": result.persona_id,
                "check_id": result.check_id,
                "status": result.status,
                "severity": result.effective_severity,
                "message": result.message,
                "location": result.location,
            }
            for result in results
        ], indent=2))
        raise typer.Exit(code=exit_code)

    by_persona: dict[str, list] = defaultdict(list)
    for result in results:
        by_persona[result.persona_id].append(result)

    failed = sum(1 for result in results if result.status == "failed")
    passed = sum(1 for result in results if result.status == "passed")
    skipped = sum(1 for result in results if result.status == "skipped")

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  Editorial Report — {edition}" + (f" issue {issue_number:03d}" if issue_number is not None else ""))
    typer.echo(f"{'=' * 60}")
    typer.echo(f"  Total checks: {len(results)}  |  passed: {passed}  |  failed: {failed}  |  skipped: {skipped}")
    typer.echo(f"{'=' * 60}\n")

    for persona_id, persona_results in sorted(by_persona.items()):
        persona_failed = [result for result in persona_results if result.status == "failed"]
        persona_passed = [result for result in persona_results if result.status == "passed"]
        status_icon = "✗" if persona_failed else "✓"
        typer.echo(f"  {status_icon} {persona_id} ({len(persona_passed)} passed, {len(persona_failed)} failed)")
        for result in persona_failed:
            typer.echo(f"      [{result.effective_severity.upper()}] {result.check_id}: {result.message}")
            if result.location:
                typer.echo(f"        → {result.location}")

    typer.echo(
        f"\n  Exit code: {exit_code} "
        f"({'clean' if exit_code == 0 else 'warnings' if exit_code == 2 else 'BLOCKING ERRORS'})"
    )
    raise typer.Exit(code=exit_code)
